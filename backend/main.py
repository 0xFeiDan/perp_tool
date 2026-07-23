from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from binance_gateway import BinanceGateway
from hyperliquid_gateway import HyperliquidGateway
from instrument_registry import InstrumentRegistry, InstrumentRegistryError
from lighter_gateway import LighterGateway, Settings, TradingError
from mt5_gateway import MT5ReadOnlyGateway
from mt5_readonly import MT5ReadOnlyError
from order_intents import OrderIntentError, OrderIntentStore, decimal_text
from repository import RepositoryError
from runtime_persistence_bridge import RuntimeExecutionClaim, RuntimePersistenceBridgeError, bridge_if_configured
from security import ControlPlane, SecuritySettings, request_fingerprint

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(Path(__file__).with_name(".env"))
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", str(Path(__file__).parent)))
RUNTIME_STATE = RUNTIME_DIR / "runtime_state.json"
AUDIT_LOG = RUNTIME_DIR / "audit.jsonl"


class SessionRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class OrderIntentRequest(BaseModel):
    venue: Literal["lighter", "hyperliquid", "binance", "mt5"] = "lighter"
    internal_instrument_id: str = Field(min_length=36, max_length=36)
    side: Literal["buy", "sell"]
    intent: Literal["open", "close"]
    mode: Literal["market", "maker"]
    notional_amount: Decimal = Field(gt=0)


class ExecuteRequest(OrderIntentRequest):
    """Trusted, server-derived execution request. Never built from browser fields."""

    confirm_live: bool = False


class ConfirmIntentRequest(BaseModel):
    order_intent_token: str = Field(min_length=32, max_length=256)
    request_id: str = Field(min_length=16, max_length=128)
    confirm_live: bool = False


@dataclass
class FollowOrder:
    venue: str
    market_id: str
    side: str
    quantity: Decimal
    reduce_only: bool
    order_id: str | None = None
    client_order_index: int | None = None
    last_price: Decimal | None = None
    state: Literal["active", "paused", "unknown"] = "active"
    failure_reason: str | None = None
    reprice_times: list[float] | None = None

    def __post_init__(self) -> None:
        self.reprice_times = []


class StrategyService:
    """One active strategy at a time; a venue/market mismatch is always rejected."""

    def __init__(self, lighter_settings: Settings):
        self.lighter_settings = lighter_settings
        self.lighter = LighterGateway(lighter_settings)
        self.hyperliquid = HyperliquidGateway()
        self.binance = BinanceGateway()
        self._venue_initialized: set[str] = set()
        self.venue = "lighter"
        self.market_id = str(lighter_settings.market_index)
        self.internal_instrument_id: str | None = None
        self.market: dict[str, Any] = {}
        self.bid: Decimal | None = None
        self.ask: Decimal | None = None
        self.bid_size: Decimal | None = None
        self.ask_size: Decimal | None = None
        self.orders: dict[str, dict[str, Any]] = {}
        self.follow: FollowOrder | None = None
        # Selection, confirmation and actual exchange submission must use the
        # same critical section.  Otherwise another authenticated browser tab
        # could switch the active contract between preview and execution.
        self.strategy_lock = asyncio.Lock()
        self.follow_lock = asyncio.Lock()
        self.events: set[asyncio.Queue] = set()
        self.stream_task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.quote_received_at: float | None = None
        self.max_quote_age_ms = max(100, min(int(os.getenv("MAX_QUOTE_AGE_MS", "1000")), 10_000))
        self.trading_enabled = os.getenv("TRADING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

    def gateway(self) -> Any:
        return {"lighter": self.lighter, "hyperliquid": self.hyperliquid, "binance": self.binance}[self.venue]

    def _gateway_for(self, venue: str) -> Any:
        return {"lighter": self.lighter, "hyperliquid": self.hyperliquid, "binance": self.binance}[venue]

    async def _ensure_venue_initialized(self, venue: str) -> None:
        """Initialize private signing state only for the venue being executed."""
        if venue in self._venue_initialized:
            return
        if venue == "lighter":
            await self.lighter.start()
        elif venue == "hyperliquid":
            await self.hyperliquid.start()
        self._venue_initialized.add(venue)

    def _live_enabled(self) -> bool:
        return self.trading_enabled and bool(self.gateway().live_trading)

    def _credentials_ready(self) -> bool:
        return bool(self.gateway().credentials_ready)

    async def start(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        restored_venue, restored_market = "lighter", str(self.lighter_settings.market_index)
        try:
            saved = json.loads(RUNTIME_STATE.read_text(encoding="utf-8"))
            restored_venue = saved.get("venue", "lighter")
            restored_market = str(saved.get("market_id", saved.get("market_index", restored_market)))
        except (ValueError, KeyError, json.JSONDecodeError, OSError):
            pass
        try:
            await self.select_market(restored_venue, restored_market, start_stream=True)
        except Exception as error:
            # A non-active exchange must never prevent the control plane from
            # booting.  The UI can still load another venue's trusted market
            # list and select it explicitly.
            self.last_error = f"初始市场暂不可用：{type(error).__name__}"

    async def close(self) -> None:
        if self.stream_task:
            self.stream_task.cancel()
            try:
                await self.stream_task
            except asyncio.CancelledError:
                pass
        await asyncio.gather(self.lighter.close(), self.hyperliquid.close(), self.binance.close())

    async def publish(self, kind: str, data: dict[str, Any]) -> None:
        payload = {"type": kind, "data": data, "timestamp": int(time.time() * 1000)}
        for queue in list(self.events):
            if not queue.full():
                queue.put_nowait(payload)

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.bid is not None and self.ask is not None,
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "bid_size": str(self.bid_size) if self.bid_size is not None else None,
            "ask_size": str(self.ask_size) if self.ask_size is not None else None,
            "venue": self.venue,
            "live_enabled": self._live_enabled(),
            "trading_enabled": self.trading_enabled,
            "venue_live_enabled": bool(self.gateway().live_trading),
            "credentials_ready": self._credentials_ready(),
            "market_id": self.market_id,
            "market_index": int(self.market_id) if self.venue == "lighter" else None,
            "internal_instrument_id": self.internal_instrument_id,
            "follow_active": bool(self.follow),
            "follow_state": self.follow.state if self.follow else None,
            "error": self.last_error,
            "symbol": self.market.get("symbol", "—"),
            "market_scope": self.market.get("market_scope", "USDT 永续" if self.venue == "binance" else "USDC 永续"),
            "min_quote_amount": self.market.get("min_quote_amount", "0"),
            "size_decimals": self.market.get("size_decimals", self.market.get("supported_size_decimals", 4)),
            "market_data_age_ms": int((time.monotonic() - self.quote_received_at) * 1000) if self.quote_received_at else None,
            "max_quote_age_ms": self.max_quote_age_ms,
        }

    async def _set_quote(self, quote: dict[str, Decimal]) -> None:
        self.bid, self.ask = quote["bid"], quote["ask"]
        self.bid_size, self.ask_size = quote["bid_size"], quote["ask_size"]
        self.quote_received_at = time.monotonic()
        await self.publish("ticker", self.snapshot())
        await self._reprice_follow()

    def _assert_fresh_quote(self) -> None:
        if self.quote_received_at is None or self.bid is None or self.ask is None:
            raise TradingError("等待有效买一/卖一行情后再下单")
        age_ms = int((time.monotonic() - self.quote_received_at) * 1000)
        if age_ms > self.max_quote_age_ms:
            raise TradingError(f"行情已过期（{age_ms}ms > {self.max_quote_age_ms}ms），请重新获取订单预览")

    @staticmethod
    def _position_meaning(intent: str, side: str) -> str:
        return {("open", "buy"): "开多", ("open", "sell"): "开空", ("close", "buy"): "平空", ("close", "sell"): "平多"}[(intent, side)]

    def _quote_currency(self) -> str:
        return "USDT" if self.venue == "binance" else "USDC"

    async def _order_terms(self, request: OrderIntentRequest | ExecuteRequest) -> tuple[Decimal, Decimal]:
        if request.venue == "mt5":
            raise TradingError("MT5_TRADING_FORBIDDEN: MT5 is a read-only data source")
        if not self.internal_instrument_id or request.internal_instrument_id != self.internal_instrument_id:
            raise TradingError("instrument does not match the active backend market; reload and select it again")
        if request.venue != self.venue:
            raise TradingError("exchange/market mismatch: refresh and select the current contract again")
        self._assert_fresh_quote()
        price = self.bid if request.side == "sell" else self.ask
        if request.mode == "maker":
            price = self.bid if request.side == "buy" else self.ask
        if price is None:
            raise TradingError("等待有效买一/卖一行情后再下单")
        if self.venue == "lighter":
            quantity = await self.lighter.quantity_from_notional(market_index=int(self.market_id), notional_usdc=request.notional_amount, price=price)
        else:
            quantity = await self.gateway().quantity_from_notional(self.market_id, request.notional_amount, price)
        return price, quantity

    async def preview_order(self, request: OrderIntentRequest) -> dict[str, Any]:
        """Server-calculated preview that is later bound into a one-time token."""
        async with self.strategy_lock:
            price, quantity = await self._order_terms(request)
            return {
                "venue": self.venue,
                "internal_instrument_id": request.internal_instrument_id,
                "market_id": self.market_id,
                "symbol": self.market.get("symbol"),
                "position_action": request.intent,
                "side": request.side,
                "position_meaning": self._position_meaning(request.intent, request.side),
                "order_mode": request.mode,
                "notional_amount": decimal_text(request.notional_amount),
                "quote_currency": self._quote_currency(),
                "estimated_quantity": decimal_text(quantity),
                "reference_price": decimal_text(price),
                "best_bid": decimal_text(self.bid),
                "best_ask": decimal_text(self.ask),
                "reduce_only": request.intent == "close",
                "post_only": request.mode == "maker",
                "time_in_force": "POST_ONLY" if request.mode == "maker" else "IOC",
                "market_data_age_ms": int((time.monotonic() - self.quote_received_at) * 1000) if self.quote_received_at else None,
            }

    async def _lighter_stream(self) -> None:
        index = int(self.market_id)
        while True:
            try:
                async with websockets.connect(self.lighter_settings.stream_url, ping_interval=45, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"ticker/{index}"}))
                    if self.lighter.client is not None:
                        token = self.lighter.create_auth_token()
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"account_orders/{index}/{self.lighter_settings.account_index}", "auth": token}))
                    self.last_error = None
                    async for raw in ws:
                        await self._handle_lighter_stream(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = str(error)
                await self.publish("system", {"message": f"Lighter行情重连中：{error}"})
                await asyncio.sleep(3)

    async def _handle_lighter_stream(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "update/ticker":
            ticker = message.get("ticker", {})
            if ticker.get("b") and ticker.get("a"):
                await self._set_quote({"bid": Decimal(ticker["b"]["price"]), "ask": Decimal(ticker["a"]["price"]), "bid_size": Decimal(ticker["b"]["size"]), "ask_size": Decimal(ticker["a"]["size"])})
        elif kind in {"subscribed/account_orders", "update/account_orders"}:
            confirmed_order_id: str | None = None
            async with self.strategy_lock:
                async with self.follow_lock:
                    for order in message.get("orders", {}).get(self.market_id, []):
                        self.orders[str(order["order_index"])] = order
                    if self.follow and self.follow.venue == "lighter" and self.follow.order_id is None:
                        for order_id, order in self.orders.items():
                            if int(order.get("client_order_index", -1)) == self.follow.client_order_index:
                                self.follow.order_id = order_id
                                confirmed_order_id = order_id
                                break
                    orders_snapshot = list(self.orders.values())
            if confirmed_order_id:
                await self.publish("system", {"message": f"Lighter跟价单已确认：#{confirmed_order_id}"})
            await self.publish("orders", {"orders": orders_snapshot})

    async def _external_poll_stream(self) -> None:
        """Consume public BBO WebSockets, with one REST recovery snapshot.

        The name remains for compatibility with older runtime state, but this
        is no longer a periodic REST polling loop.  Each selected external
        venue owns its stream until cancellation on market switch.
        """
        venue, market_id, gateway = self.venue, self.market_id, self.gateway()
        while True:
            try:
                # A one-time REST snapshot gives the UI a safe BBO while a
                # WebSocket is connecting/recovering; it is never used as a
                # high-frequency order-pricing poll.
                try:
                    quote = await gateway.bbo(market_id)
                    if self.venue == venue and self.market_id == market_id:
                        await self._set_quote(quote)
                except Exception as snapshot_error:
                    self.last_error = f"{venue} BBO recovery snapshot unavailable: {type(snapshot_error).__name__}"
                    await self.publish("system", {"message": self.last_error})

                async for quote in gateway.stream_bbo(market_id):
                    if self.venue != venue or self.market_id != market_id:
                        return
                    self.last_error = None
                    await self._set_quote(quote)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = f"{venue} BBO stream unavailable: {type(error).__name__}"
                await self.publish("system", {"message": f"{venue}行情流重连中：{type(error).__name__}"})
                await asyncio.sleep(1)

    async def _reprice_follow(self) -> None:
        async with self.strategy_lock:
            async with self.follow_lock:
                follow = self.follow
                if not follow or follow.venue != self.venue or follow.market_id != self.market_id or follow.order_id is None:
                    return
                if follow.state != "active":
                    # An ambiguous exchange response or rate-limit stop is
                    # intentionally sticky.  It must be reconciled/cancelled
                    # before this strategy may be changed or resumed.
                    return
                if not self.trading_enabled:
                    # A global kill switch must stop all automatic writes while
                    # still allowing the user to submit an explicit cancellation.
                    return
                desired = self.bid if follow.side == "buy" else self.ask
                if desired is None or desired == follow.last_price:
                    return
                now = time.monotonic()
                follow.reprice_times[:] = [stamp for stamp in follow.reprice_times if now - stamp < 60]
                if len(follow.reprice_times) >= self.lighter_settings.max_reprices_per_minute:
                    follow.state = "paused"
                    follow.failure_reason = "follow reprice rate limit reached"
                    await self.publish("system", {"message": "跟价改单频率保护已触发；订单已保留并暂停，需撤单或核对交易所后才能切换策略"})
                    return
                try:
                    if self.venue == "lighter":
                        await self.lighter.modify_order(market_index=int(self.market_id), order_index=int(follow.order_id), quantity=follow.quantity, price=desired)
                    elif self.venue == "hyperliquid":
                        result = await self.hyperliquid.modify_order(coin=self.market_id, order_id=follow.order_id, side=follow.side, quantity=follow.quantity, price=desired, reduce_only=follow.reduce_only)
                        follow.order_id = result["order_id"]
                    else:
                        # Both external venues use cancel/repost when the exchange has no
                        # portable atomic replace API.  The exact tracked ID is cancelled first.
                        await self.gateway().cancel_order(self.market_id, follow.order_id)
                        result = await self.binance.create_order(symbol=self.market_id, side=follow.side, quantity=follow.quantity, price=desired, reduce_only=follow.reduce_only, maker=True, client_id=f"bbof-{uuid.uuid4().hex[:20]}")
                        follow.order_id = result.get("order_id")
                        if not follow.order_id:
                            raise TradingError("exchange did not return a follow-order id")
                    follow.last_price = desired
                    follow.reprice_times.append(now)
                    await self.publish("system", {"message": f"{self.venue}跟价更新：{desired}"})
                except TradingError as error:
                    # Never discard an order identifier after an uncertain
                    # exchange result.  Keep the strategy blocked and require
                    # an explicit cancel/reconciliation instead.
                    follow.state = "unknown"
                    follow.failure_reason = str(error)[:160]
                    await self.publish("system", {"message": f"跟价状态未知，已停止自动改价；请核对并撤销指定订单后再切换：{error}"})

    async def execute(
        self,
        request: ExecuteRequest,
        *,
        expected_price: Decimal,
        expected_quantity: Decimal,
        client_order_id: str | None = None,
        before_submit: Callable[[Decimal, Decimal, str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Submit a server-derived order after the optional durable pre-write.

        ``before_submit`` runs while the active-instrument lock is held and
        only after the fresh BBO/quantity recheck succeeds. A persistence
        failure therefore happens before any exchange adapter is called.
        """
        async with self.strategy_lock:
            if not self.trading_enabled:
                raise TradingError("TRADING_ENABLED=false: real execution is locked")
            if not self.gateway().live_trading:
                raise TradingError("the selected exchange has *_LIVE_TRADING=false")
            if not self._credentials_ready():
                raise TradingError("the selected exchange credentials are not ready for real execution")
            if not request.confirm_live:
                raise TradingError("必须在界面确认真实执行")
            price, quantity = await self._order_terms(request)
            # The confirmation dialog must describe the exact order being sent.
            # Any BBO move, precision change, or active-instrument change
            # requires a fresh, one-time preview.
            if price != expected_price or quantity != expected_quantity:
                raise TradingError("盘口或预估数量已变化，请重新生成订单预览并确认")
            safe_client_order_id = client_order_id or f"bbo-{uuid.uuid4().hex[:22]}"
            if before_submit:
                await before_submit(price, quantity, safe_client_order_id)
            if self.venue == "lighter":
                await self._ensure_venue_initialized(self.venue)
                result = await self.lighter.create_bbo_order(market_index=int(self.market_id), side=request.side, quantity=quantity, bbo_price=price, reduce_only=request.intent == "close", maker=request.mode == "maker", client_id=safe_client_order_id)
                order_id, client_index = None, result["client_order_index"]
            else:
                await self._ensure_venue_initialized(self.venue)
                kwargs = {"symbol": self.market_id} if self.venue == "binance" else {"coin": self.market_id}
                result = await self.gateway().create_order(**kwargs, side=request.side, quantity=quantity, price=price, reduce_only=request.intent == "close", maker=request.mode == "maker", client_id=safe_client_order_id)
                order_id, client_index = result.get("order_id"), None
                if request.mode == "maker" and not order_id:
                    raise TradingError("exchange did not confirm a resting maker order")
            if request.mode == "maker":
                async with self.follow_lock:
                    self.follow = FollowOrder(self.venue, self.market_id, request.side, quantity, request.intent == "close", order_id=order_id, client_order_index=client_index, last_price=price)
            payload = {"venue": self.venue, "market_id": self.market_id, "symbol": self.market.get("symbol"), "price": str(price), "quantity": str(quantity), "notional_amount": str(request.notional_amount), "quote_currency": self._quote_currency(), "client_order_id": safe_client_order_id, **result}
            await self.publish("execution", {"mode": request.mode, "side": request.side, "intent": request.intent, **payload})
            return payload

    async def cancel_follow(self) -> dict[str, Any]:
        async with self.strategy_lock:
            async with self.follow_lock:
                if not self.follow:
                    return {"canceled": False}
                follow = self.follow
                if follow.order_id is None:
                    raise TradingError("跟价单尚未得到交易所订单号，拒绝盲目撤单")
                if follow.venue == "lighter":
                    result = await self.lighter.cancel_order(market_index=int(follow.market_id), order_index=int(follow.order_id))
                else:
                    result = await {"binance": self.binance, "hyperliquid": self.hyperliquid}[follow.venue].cancel_order(follow.market_id, follow.order_id)
                self.follow = None
                await self.publish("system", {"message": "已提交指定跟价单撤单"})
                return {"canceled": True, **result}

    async def markets(self, venue: str) -> list[dict[str, Any]]:
        if venue == "lighter":
            raw = await self.lighter.list_markets()
            return [{
                "market_id": str(x["market_id"]),
                "market_index": int(x["market_id"]),
                "symbol": x["symbol"],
                "base_asset": x["symbol"],
                "quote_asset": "USDC",
                "settle_asset": "USDC",
                "market_scope": "USDC 永续",
                "min_quote_amount": x["min_quote_amount"],
                "size_decimals": x.get("supported_size_decimals", x.get("size_decimals", 4)),
            } for x in sorted(raw, key=lambda item: item["symbol"])]
        return await {"hyperliquid": self.hyperliquid, "binance": self.binance}[venue].markets()

    async def select_market(self, venue: str, market_id: str, start_stream: bool = False, internal_instrument_id: str | None = None) -> None:
        if venue not in {"lighter", "hyperliquid", "binance"}:
            raise TradingError("unsupported exchange")
        async with self.strategy_lock:
            async with self.follow_lock:
                if self.follow:
                    raise TradingError("请先取消活跃跟价单，再切换交易所或合约")
            # Fetch and validate target metadata before disrupting the active
            # quote stream.  A broken non-active venue therefore leaves the
            # existing strategy untouched.
            target_market_id = str(market_id)
            if venue == "lighter":
                raw = await self.lighter.refresh_market_metadata(int(target_market_id))
                target_market = {
                    "market_id": target_market_id,
                    "symbol": raw["symbol"],
                    "quote_asset": "USDC",
                    "settle_asset": "USDC",
                    "market_scope": "USDC 永续",
                    "min_quote_amount": raw["min_quote_amount"],
                    "size_decimals": raw.get("supported_size_decimals", raw.get("size_decimals", 4)),
                }
                # When this *active* venue is explicitly armed, initialize its
                # signer before opening the stream so Lighter account-order
                # acknowledgements can be subscribed safely. An inactive
                # exchange never performs this private-key validation.
                if self.lighter_settings.live_trading and self.lighter_settings.credentials_ready:
                    await self._ensure_venue_initialized(venue)
            else:
                target_market = await self._gateway_for(venue).market(target_market_id)
            if self.stream_task and not start_stream:
                self.stream_task.cancel()
                try:
                    await self.stream_task
                except asyncio.CancelledError:
                    pass
            self.venue, self.market_id = venue, target_market_id
            self.market = target_market
            task = self._lighter_stream() if venue == "lighter" else self._external_poll_stream()
            # Bind the opaque instrument ID in the same critical section as
            # the external market switch; no old intent can observe a new
            # market while retaining the previous internal identifier.
            self.internal_instrument_id = internal_instrument_id
            RUNTIME_STATE.write_text(json.dumps({"venue": venue, "market_id": self.market_id}), encoding="utf-8")
            self.bid = self.ask = self.bid_size = self.ask_size = None
            self.orders = {}
            self.stream_task = asyncio.create_task(task, name=f"{venue}-stream-{self.market_id}")
            await self.publish("market", self.snapshot())

    async def bind_active_instrument(self, venue: str, market_id: str, internal_instrument_id: str) -> str | None:
        """Bind restored state only if it still names the expected contract."""
        async with self.strategy_lock:
            if self.venue != venue or self.market_id != str(market_id):
                return None
            self.internal_instrument_id = internal_instrument_id
            return self.internal_instrument_id


lighter_settings = Settings.from_env()
security_settings = SecuritySettings.from_env()
control = ControlPlane(security_settings, AUDIT_LOG)
intent_store = OrderIntentStore(ttl_seconds=int(os.getenv("ORDER_INTENT_TTL_SECONDS", "30")))
service = StrategyService(lighter_settings)
instrument_registry = InstrumentRegistry()
mt5_gateway = MT5ReadOnlyGateway()
# A production trading process must keep the confirmation, idempotency and
# pre-submission records in PostgreSQL.  Read-only local development can omit
# DATABASE_URL without weakening the backend's global TRADING_ENABLED=false
# default.
durable_execution_required = os.getenv("DURABLE_EXECUTION_REQUIRED", "true").strip().lower() in {"1", "true", "yes", "on"}
persistence_bridge = bridge_if_configured(os.getenv("DATABASE_URL", "").strip() or None)
persistence_ready = not persistence_bridge.enabled
persistence_error: str | None = None


def execution_client_order_id(persistent_intent_id: str | None) -> str:
    """Make one exchange-safe client ID without exposing browser identifiers."""
    if persistent_intent_id:
        return f"bbo-{persistent_intent_id.replace('-', '')[:24]}"
    return f"bbo-{uuid.uuid4().hex[:22]}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global persistence_ready, persistence_error
    if any("*" in host for host in security_settings.allowed_hosts) or any("*" in origin for origin in security_settings.allowed_origins):
        raise RuntimeError("ALLOWED_HOSTS and ALLOWED_ORIGINS must be exact values; wildcards are forbidden")
    if security_settings.public_https and any(not origin.startswith("https://") for origin in security_settings.allowed_origins):
        raise RuntimeError("PUBLIC_HTTPS=true requires HTTPS-only ALLOWED_ORIGINS")
    if service.trading_enabled and not security_settings.public_https:
        loopback_hosts = {"127.0.0.1", "localhost", "[::1]"}
        if any(host not in loopback_hosts for host in security_settings.allowed_hosts):
            raise RuntimeError("real execution outside loopback requires PUBLIC_HTTPS=true and an exact HTTPS origin")
    if any((lighter_settings.live_trading, service.hyperliquid.live_trading, service.binance.live_trading)) and not security_settings.configured:
        raise RuntimeError("A 32+ character CONTROL_PLANE_TOKEN is required whenever any live venue is enabled")
    if service.trading_enabled and durable_execution_required and not persistence_bridge.enabled:
        raise RuntimeError("TRADING_ENABLED=true requires DATABASE_URL when DURABLE_EXECUTION_REQUIRED=true")
    if persistence_bridge.enabled:
        try:
            await asyncio.to_thread(persistence_bridge.assert_ready)
            persistence_ready, persistence_error = True, None
        except Exception as error:
            persistence_ready = False
            persistence_error = type(error).__name__
            if service.trading_enabled and durable_execution_required:
                raise RuntimeError("durable execution storage is unavailable; real execution remains locked") from error
    await service.start()
    # The local Wine adapter or its Unix-socket sidecar both fail closed when
    # investor-mode permissions cannot be verified.  A telemetry-only remote
    # sidecar being down never enables trading or blocks exchange safeguards.
    await mt5_gateway.start()
    yield
    await mt5_gateway.close()
    await service.close()
    await asyncio.to_thread(persistence_bridge.dispose)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        # ``ws:``/``wss:`` keep the same-origin dashboard stream compatible
        # across local HTTP and Tailnet HTTPS deployments.  The application
        # itself constructs only a same-origin WebSocket and the server still
        # requires the authenticated cookie plus an exact Origin allowlist.
        response.headers["Content-Security-Policy"] = "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'; script-src 'self'"
        if security_settings.public_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app = FastAPI(title="BBO Strategy Control Plane", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(security_settings.allowed_hosts))
app.add_middleware(CORSMiddleware, allow_origins=list(security_settings.allowed_origins), allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-CSRF-Token"])
@app.get("/")
async def home(): return FileResponse(ROOT / "index.html")

@app.get("/styles.css")
async def styles(): return FileResponse(ROOT / "styles.css", media_type="text/css")

@app.get("/mt5-readonly.css")
async def mt5_styles(): return FileResponse(ROOT / "mt5-readonly.css", media_type="text/css")

@app.get("/app.js")
async def frontend_script(): return FileResponse(ROOT / "app.js", media_type="application/javascript")

@app.get("/healthz")
async def healthz():
    """Unauthenticated container health check; intentionally reveals no account data."""
    return {"status": "ok", "service": "bbo-strategy"}

@app.get("/api/session")
async def session_status(request: Request): return control.session_status(request)

@app.post("/api/session")
async def create_session(request: Request, body: SessionRequest, response: Response):
    client_key = control.client_key(request)
    try:
        session, csrf, expires = control.create_session(body.token, client_key)
    except HTTPException as error:
        control.audit("session_login_rejected", ip=client_key, status=error.status_code)
        raise
    response.set_cookie("strategy_session", session, max_age=security_settings.session_minutes * 60, httponly=True, secure=security_settings.public_https, samesite="strict", path="/")
    control.audit("session_created", ip=client_key)
    return {"csrf": csrf, "expires": expires}

@app.post("/api/session/logout")
async def logout(request: Request, response: Response):
    control.require_http(request, write=True)
    control.revoke_session(request)
    response.delete_cookie("strategy_session", path="/")
    control.audit("session_logout", ip=request.client.host if request.client else "unknown")
    return {"ok": True}

@app.get("/api/health")
async def health(request: Request):
    control.require_http(request)
    return {
        **service.snapshot(),
        "control_plane": control.session_status(request),
        "durable_execution": {
            "configured": persistence_bridge.enabled,
            "ready": persistence_ready,
            "required": durable_execution_required,
            "error": persistence_error,
        },
        "mt5_readonly": await mt5_gateway.status(),
    }

@app.get("/api/orders")
async def orders(request: Request):
    control.require_http(request)
    return {
        "orders": list(service.orders.values()),
        "follow_active": bool(service.follow),
        "follow_state": service.follow.state if service.follow else None,
        "follow_failure_reason": service.follow.failure_reason if service.follow else None,
    }

@app.get("/api/portfolio")
async def portfolio(request: Request):
    """Return real balances plus a durable USDC/USDT 1:1 equity timeline."""
    control.require_http(request)
    start_date = os.getenv("PORTFOLIO_START_DATE", "2026-07-21").strip() or "2026-07-21"
    try:
        history_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        # Keep a malformed optional setting from blocking account reads.
        start_date = "2026-07-21"
        history_start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    definitions = [
        ("lighter", "Lighter", "USDC", service.lighter.credentials_ready, service.lighter.portfolio_snapshot),
        ("hyperliquid", "Hyperliquid", "USDC", service.hyperliquid.credentials_ready, service.hyperliquid.portfolio_snapshot),
        ("binance", "Binance USD-M", "USDT", service.binance.credentials_ready, service.binance.portfolio_snapshot),
    ]
    reads = await asyncio.gather(*(reader() if configured else _portfolio_not_configured() for _, _, _, configured, reader in definitions), return_exceptions=True)
    venues: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    totals = {"equity": Decimal("0"), "available_margin": Decimal("0"), "unrealized_pnl": Decimal("0")}
    synced = 0
    for (venue, label, currency, configured, _), result in zip(definitions, reads, strict=True):
        item: dict[str, Any] = {"venue": venue, "label": label, "currency": currency, "configured": configured, "synced": False}
        if not configured:
            item["status"] = "未配置账户 API"
        elif isinstance(result, Exception):
            # Do not return raw upstream bodies: they can contain account IDs,
            # signatures or provider-specific diagnostic material.
            item["status"] = "读取失败"
            item["error_type"] = type(result).__name__
        elif isinstance(result, dict):
            try:
                equity = Decimal(str(result["equity"]))
                available = Decimal(str(result["available_margin"]))
                pnl = Decimal(str(result["unrealized_pnl"]))
                if not all(value.is_finite() for value in (equity, available, pnl)) or equity < 0 or available < 0:
                    raise ValueError("non-finite portfolio total")
            except (KeyError, ArithmeticError, ValueError):
                item["status"] = "读取失败"
                item["error_type"] = "InvalidPortfolioResponse"
            else:
                item.update({
                    "synced": True,
                    "status": "已同步",
                    "equity": format(equity, "f"),
                    "available_margin": format(available, "f"),
                    "unrealized_pnl": format(pnl, "f"),
                })
                # User-confirmed reporting convention: 1 USDT = 1 USDC.
                totals["equity"] += equity
                totals["available_margin"] += available
                totals["unrealized_pnl"] += pnl
                for position in result.get("positions", []):
                    if isinstance(position, dict):
                        positions.append(position)
                synced += 1
        else:
            item["status"] = "读取失败"
            item["error_type"] = "InvalidPortfolioResponse"
        venues.append(item)

    observed_at = datetime.now(timezone.utc)
    history: list[dict[str, Any]] = []
    history_status = "未启用持久化"
    if synced:
        try:
            snapshot = await asyncio.to_thread(
                persistence_bridge.record_portfolio_snapshot,
                observed_at=observed_at,
                total_equity=totals["equity"],
                available_margin=totals["available_margin"],
                unrealized_pnl=totals["unrealized_pnl"],
                synced_venues=synced,
            )
            records = await asyncio.to_thread(persistence_bridge.portfolio_history, start_at=history_start)
            if snapshot is None:
                # Local read-only mode still renders the current verified point,
                # but correctly labels that it cannot survive a restart.
                records = []
                history_status = "当前会话快照（未持久化）"
            else:
                history_status = "已持久化"
            history = [
                {
                    "timestamp": int(record.observed_at.timestamp() * 1000),
                    "equity": format(record.total_equity, "f"),
                    "available_margin": format(record.available_margin, "f"),
                    "unrealized_pnl": format(record.unrealized_pnl, "f"),
                    "synced_venues": record.synced_venues,
                }
                for record in records
            ]
        except Exception:
            # Read failures must not conceal the live balances shown above.
            history_status = "历史快照暂不可用"
        if not history:
            history = [{
                "timestamp": int(observed_at.timestamp() * 1000),
                "equity": format(totals["equity"], "f"),
                "available_margin": format(totals["available_margin"], "f"),
                "unrealized_pnl": format(totals["unrealized_pnl"], "f"),
                "synced_venues": synced,
            }]
    summary = {name: format(value, "f") for name, value in totals.items()}
    notice = (
        f"已同步 {synced} 个交易所的当前账户数据；按你的约定，USDC 与 USDT 以 1:1 合并统计。权益快照从今天立即开始保存。"
        if synced else "尚未读取到可验证的账户数据；请检查对应交易所的账户 API 配置。"
    )
    return {
        "from_date": start_date,
        "as_of": int(observed_at.timestamp() * 1000),
        "currency": "USDC/USDT",
        "currency_assumption": "1 USDC = 1 USDT",
        "summary": summary,
        "history": history,
        "history_status": history_status,
        "positions": positions,
        "venues": venues,
        "notice": notice,
    }


async def _portfolio_not_configured() -> None:
    """Keep gather's execution shape uniform without touching an exchange."""
    return None

@app.get("/api/markets")
async def markets(request: Request, venue: Literal["lighter", "hyperliquid", "binance"] = "lighter"):
    control.require_http(request)
    trusted_markets = instrument_registry.sync(venue, await service.markets(venue))
    selected_internal_instrument_id = None
    if venue == service.venue:
        internal_instrument_id = instrument_registry.internal_id_for(venue=venue, external_id=service.market_id)
        if internal_instrument_id:
            selected_internal_instrument_id = await service.bind_active_instrument(venue, service.market_id, internal_instrument_id)
    return {"venue": venue, "markets": trusted_markets, "selected_venue": service.venue, "selected_market_id": service.market_id, "selected_internal_instrument_id": selected_internal_instrument_id}

@app.post("/api/market/{venue}/{internal_instrument_id}")
async def select_market(venue: Literal["lighter", "hyperliquid", "binance", "mt5"], internal_instrument_id: str, request: Request):
    control.require_http(request, write=True)
    try:
        if venue == "mt5":
            raise TradingError("MT5_TRADING_FORBIDDEN: MT5 cannot be selected in the trading workflow")
        instrument = instrument_registry.resolve(venue=venue, internal_instrument_id=internal_instrument_id)
        await service.select_market(venue, instrument.external_id, internal_instrument_id=instrument.internal_instrument_id)
        control.audit("market_selected", venue=venue, internal_instrument_id=instrument.internal_instrument_id, exchange_symbol=instrument.external_id)
        return service.snapshot()
    except (TradingError, InstrumentRegistryError) as error:
        raise HTTPException(status_code=400, detail=str(error))

@app.get("/api/mt5/status")
async def mt5_status(request: Request):
    control.require_http(request)
    return await mt5_gateway.status()

@app.get("/api/mt5/account")
async def mt5_account(request: Request):
    control.require_http(request)
    try:
        return {"account": await mt5_gateway.account_info(), "status": await mt5_gateway.status()}
    except MT5ReadOnlyError as error:
        raise HTTPException(status_code=503, detail=str(error))

@app.get("/api/mt5/positions")
async def mt5_positions(request: Request):
    control.require_http(request)
    try:
        return {"positions": await mt5_gateway.positions(), "status": await mt5_gateway.status()}
    except MT5ReadOnlyError as error:
        raise HTTPException(status_code=503, detail=str(error))

@app.get("/api/mt5/quote/{symbol}")
async def mt5_quote(symbol: str, request: Request):
    control.require_http(request)
    try:
        return {"symbol": symbol, "quote": await mt5_gateway.quote(symbol), "status": await mt5_gateway.status()}
    except MT5ReadOnlyError as error:
        raise HTTPException(status_code=503, detail=str(error))

@app.post("/api/order-intents")
async def create_order_intent(request: Request, body: OrderIntentRequest):
    """Generate a single-use server-side preview before showing confirmation."""
    control.require_http(request, write=True, execute=True)
    intent = None
    try:
        if persistence_bridge.enabled and not persistence_ready:
            raise RuntimePersistenceBridgeError("durable execution storage is unavailable")
        if body.venue == "mt5":
            raise TradingError("MT5_TRADING_FORBIDDEN: MT5 is a read-only data source")
        instrument = instrument_registry.resolve(venue=body.venue, internal_instrument_id=body.internal_instrument_id)
        if service.internal_instrument_id != instrument.internal_instrument_id or service.market_id != instrument.external_id:
            raise TradingError("instrument does not match the active backend market; reload and select it again")
        preview = await service.preview_order(body)
        intent = intent_store.issue(
            owner=control.authenticated_owner(request),
            payload={"request": body.model_dump(mode="json"), "preview": preview},
        )
        # Persist only a hash of this opaque token.  The dataclass context is
        # retained solely inside the in-memory token payload and is never sent
        # to the browser with the preview.
        intent.payload["persistence_context"] = await asyncio.to_thread(
            persistence_bridge.prepare_intent,
            venue=body.venue,
            internal_instrument=instrument,
            side=body.side,
            intent=body.intent,
            mode=body.mode,
            notional=body.notional_amount,
            raw_token=intent.token,
            expires_in_seconds=intent_store.ttl_seconds,
        )
        control.audit(
            "order_intent_created",
            venue=body.venue,
            internal_instrument_id=body.internal_instrument_id,
            exchange_symbol=instrument.external_id,
            side=body.side,
            intent=body.intent,
            mode=body.mode,
            notional_amount=str(body.notional_amount),
            request=request_fingerprint(body.model_dump()),
        )
        return intent.public_preview()
    except (TradingError, InstrumentRegistryError) as error:
        control.audit("order_intent_rejected", venue=body.venue, internal_instrument_id=body.internal_instrument_id, reason=str(error)[:160])
        raise HTTPException(status_code=400, detail=str(error))
    except (RuntimePersistenceBridgeError, RepositoryError) as error:
        if intent is not None:
            intent_store.discard(token=intent.token)
        control.audit("order_intent_storage_rejected", venue=body.venue, internal_instrument_id=body.internal_instrument_id, reason=type(error).__name__)
        raise HTTPException(status_code=503, detail="durable execution storage is unavailable; no order intent was issued")

@app.post("/api/execute")
async def execute(request: Request, body: ConfirmIntentRequest):
    control.require_http(request, write=True, execute=True)
    control.claim_idempotency(body.request_id)
    durable_claim: RuntimeExecutionClaim | None = None
    durable_order: Any | None = None
    try:
        intent = intent_store.consume(token=body.order_intent_token, owner=control.authenticated_owner(request))
        persistence_context = intent.payload.get("persistence_context")
        if persistence_context is None:
            raise OrderIntentError("order intent is missing its server-side durable context; create a new preview")
        durable_claim = await asyncio.to_thread(
            persistence_bridge.claim_execution,
            persistence_context,
            request_id=body.request_id,
        )
        if not durable_claim.claimed:
            raise OrderIntentError("duplicate execution request is pending or has already been processed; do not resend")
        submission = await asyncio.to_thread(
            persistence_bridge.consume_for_submission,
            persistence_context,
            raw_token=body.order_intent_token,
        )
        execution = ExecuteRequest(**intent.payload["request"], confirm_live=body.confirm_live)
        preview = intent.payload["preview"]

        async def before_submit(price: Decimal, quantity: Decimal, client_order_id: str) -> None:
            nonlocal durable_order
            durable_order = await asyncio.to_thread(
                persistence_bridge.pre_submit_order,
                submission,
                client_order_id=client_order_id,
                quantity=quantity,
                limit_price=price,
            )

        result = await service.execute(
            execution,
            expected_price=Decimal(preview["reference_price"]),
            expected_quantity=Decimal(preview["estimated_quantity"]),
            client_order_id=execution_client_order_id(persistence_context.persistent_intent_id),
            before_submit=before_submit,
        )
        if durable_order is None:
            # Defensive: the execution method must never reach an adapter
            # without running the pre-submit callback.
            raise RuntimePersistenceBridgeError("exchange result returned without a durable pre-submit order")
        durable_order = await asyncio.to_thread(
            persistence_bridge.mark_order_submitted,
            durable_order,
            exchange_order_id=result.get("order_id"),
        )
        durable_claim = await asyncio.to_thread(
            persistence_bridge.complete_execution,
            durable_claim,
            status="completed",
            response_reference=durable_order.persistent_order_id,
        )
        control.audit("execute_submitted", venue=execution.venue, internal_instrument_id=execution.internal_instrument_id, exchange_symbol=result["market_id"], side=execution.side, intent=execution.intent, mode=execution.mode, notional_amount=str(execution.notional_amount), request=request_fingerprint(body.model_dump()), result="accepted")
        return result
    except (TradingError, OrderIntentError) as error:
        # Once the local pre-submit record exists, any adapter failure is
        # treated as unknown until an explicit exchange reconciliation proves
        # otherwise.  Retrying this browser confirmation is intentionally
        # blocked by the consumed token and idempotency claim.
        if durable_order is not None:
            with suppress(Exception):
                await asyncio.to_thread(
                    persistence_bridge.record_order_status,
                    durable_order,
                    status="unknown",
                    audit_metadata={"error_type": type(error).__name__},
                )
        if durable_claim is not None:
            with suppress(Exception):
                await asyncio.to_thread(
                    persistence_bridge.complete_execution,
                    durable_claim,
                    status="unknown" if durable_order is not None else "rejected",
                    response_reference=getattr(durable_order, "persistent_order_id", None),
                )
        control.audit("execute_rejected", request=request_fingerprint(body.model_dump()), reason=str(error)[:160])
        if durable_order is not None:
            raise HTTPException(status_code=503, detail="order submission status is unknown; reconcile the exchange order before any retry")
        raise HTTPException(status_code=400, detail=str(error))
    except (RuntimePersistenceBridgeError, RepositoryError) as error:
        if durable_order is not None:
            with suppress(Exception):
                await asyncio.to_thread(
                    persistence_bridge.record_order_status,
                    durable_order,
                    status="unknown",
                    audit_metadata={"error_type": type(error).__name__},
                )
        if durable_claim is not None:
            with suppress(Exception):
                await asyncio.to_thread(
                    persistence_bridge.complete_execution,
                    durable_claim,
                    status="unknown" if durable_order is not None else "rejected",
                    response_reference=getattr(durable_order, "persistent_order_id", None),
                )
        control.audit("execute_storage_rejected", request=request_fingerprint(body.model_dump()), reason=type(error).__name__)
        if durable_order is not None:
            raise HTTPException(status_code=503, detail="order may have reached the exchange; reconcile it before any retry")
        raise HTTPException(status_code=503, detail="durable execution storage rejected the request; no exchange order was sent")

@app.post("/api/cancel-follow")
async def cancel_follow(request: Request):
    control.require_http(request, write=True, execute=True)
    try:
        result = await service.cancel_follow()
        control.audit("follow_cancel", venue=service.venue, market_id=service.market_id, result="accepted")
        return result
    except TradingError as error:
        control.audit("follow_cancel_rejected", venue=service.venue, market_id=service.market_id, reason=str(error)[:160])
        raise HTTPException(status_code=400, detail=str(error))

@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket):
    if not await control.require_websocket(socket):
        return
    await socket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    service.events.add(queue)
    try:
        await socket.send_json({"type": "ticker", "data": service.snapshot()})
        while True:
            await socket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        service.events.discard(queue)
