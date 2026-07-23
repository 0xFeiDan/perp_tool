"""Hyperliquid perpetual adapter using the official Python SDK for signing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

import httpx
import websockets

from lighter_gateway import ExecutionUncertainError, TradingError


class HyperliquidGateway:
    venue = "hyperliquid"
    info_url = "https://api.hyperliquid.xyz/info"

    def __init__(self) -> None:
        self.account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
        self.wallet_private_key = os.getenv("HYPERLIQUID_API_WALLET_PRIVATE_KEY", "").strip()
        self.live_trading = os.getenv("HYPERLIQUID_LIVE_TRADING", "false").lower() == "true"
        # This is a public market-data socket.  It sends no API wallet or
        # account address and can be used while all execution remains locked.
        self.ws_url = os.getenv("HYPERLIQUID_WS_URL", "wss://api.hyperliquid.xyz/ws").rstrip("/")
        self.http = httpx.AsyncClient(timeout=10)
        self.metadata: dict[str, dict[str, Any]] = {}
        self.exchange: Any = None
        self.last_stream_error: str | None = None
        self._stream_reconnect_initial_seconds = 1.0
        self._stream_reconnect_max_seconds = 15.0

    @property
    def credentials_ready(self) -> bool:
        return bool(self.account_address and self.wallet_private_key)

    async def start(self) -> None:
        if not self.credentials_ready:
            return
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except ImportError as error:
            raise TradingError("Hyperliquid SDK is missing; install backend requirements") from error
        wallet = Account.from_key(self.wallet_private_key)
        self.exchange = Exchange(wallet, base_url="https://api.hyperliquid.xyz", account_address=self.account_address)

    async def close(self) -> None:
        await self.http.aclose()

    async def _info(self, payload: dict[str, Any]) -> Any:
        response = await self.http.post(self.info_url, json=payload)
        response.raise_for_status(); return response.json()

    async def markets(self) -> list[dict[str, Any]]:
        # ``meta`` without a dex selector is Hyperliquid's primary perpetual
        # DEX. Its collateral is USDC; spot and builder-deployed DEX markets
        # are intentionally not mixed into this selector.
        metadata = await self._info({"type": "meta"})
        result: list[dict[str, Any]] = []
        for index, item in enumerate(metadata.get("universe", [])):
            if item.get("isDelisted") or not str(item.get("name", "")).strip():
                continue
            market = {
                "market_id": item["name"],
                "symbol": item["name"],
                "base_asset": item["name"],
                "quote_asset": "USDC",
                "settle_asset": "USDC",
                "market_scope": "USDC 永续",
                "asset_index": index,
                "size_decimals": int(item.get("szDecimals", 0)),
                "min_quote_amount": "10",
            }
            self.metadata[item["name"]] = market
            result.append(market)
        return sorted(result, key=lambda value: value["symbol"])

    async def market(self, coin: str) -> dict[str, Any]:
        return self.metadata.get(coin) or next((value for value in await self.markets() if value["market_id"] == coin), None) or (_ for _ in ()).throw(TradingError("Hyperliquid market not found"))

    async def bbo(self, coin: str) -> dict[str, Decimal]:
        """REST snapshot fallback; the primary pricing source is ``stream_bbo``."""
        data = await self._info({"type": "l2Book", "coin": coin})
        quote = self._quote_from_levels(data.get("levels"))
        if quote is None:
            raise TradingError("Hyperliquid has no valid BBO")
        return quote

    @staticmethod
    def _quote_from_best_levels(bid: object, ask: object) -> dict[str, Decimal] | None:
        """Validate one best-level pair shared by BBO and L2 book messages."""
        if not isinstance(bid, dict) or not isinstance(ask, dict):
            return None
        try:
            quote = {
                "bid": Decimal(str(bid.get("px"))),
                "ask": Decimal(str(ask.get("px"))),
                "bid_size": Decimal(str(bid.get("sz"))),
                "ask_size": Decimal(str(ask.get("sz"))),
            }
        except (InvalidOperation, TypeError, ValueError):
            return None
        if (
            not all(value.is_finite() for value in quote.values())
            or quote["bid"] <= 0
            or quote["ask"] <= 0
            or quote["bid"] >= quote["ask"]
            or quote["bid_size"] < 0
            or quote["ask_size"] < 0
        ):
            return None
        return quote

    @classmethod
    def _quote_from_levels(cls, levels: object) -> dict[str, Decimal] | None:
        """Return only a non-crossed first bid/ask from a Hyperliquid L2 book."""
        if not isinstance(levels, list) or len(levels) < 2:
            return None
        bids, asks = levels[0], levels[1]
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            return None
        return cls._quote_from_best_levels(bids[0], asks[0])

    @classmethod
    def _quote_from_bbo(cls, bbo: object) -> dict[str, Decimal] | None:
        if not isinstance(bbo, list) or len(bbo) < 2:
            return None
        return cls._quote_from_best_levels(bbo[0], bbo[1])

    @classmethod
    def _parse_bbo_message(cls, message: str | bytes | dict[str, Any], expected_coin: str) -> dict[str, Decimal] | None:
        """Parse official ``bbo`` data, with ``l2Book`` support for recovery."""
        try:
            payload = json.loads(message.decode("utf-8") if isinstance(message, bytes) else message) if not isinstance(message, dict) else message
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        channel = payload.get("channel")
        data = payload.get("data")
        if channel not in {"bbo", "l2Book"} or not isinstance(data, dict):
            return None
        if str(data.get("coin", "")) != expected_coin:
            return None
        # ``bbo`` is the low-bandwidth official stream. ``l2Book`` parsing is
        # deliberately retained so a caller can safely process a snapshot if
        # Hyperliquid sends one while recovering a subscription.
        return cls._quote_from_bbo(data.get("bbo")) if channel == "bbo" else cls._quote_from_levels(data.get("levels"))

    async def stream_bbo(self, coin: str) -> AsyncIterator[dict[str, Decimal]]:
        """Yield validated public Hyperliquid BBO updates with reconnects.

        The official BBO subscription is chosen over polling ``l2Book``.  It
        contains the same first-level price/size contract needed by the
        strategy while avoiding a REST request every 0.75 seconds.  Any server
        disconnect is retried with bounded backoff; caller cancellation remains
        immediate and there is no signing/private-wallet interaction here.
        """
        normalized = coin.strip()
        if not normalized or len(normalized) > 128:
            raise TradingError("invalid Hyperliquid coin for BBO stream")
        subscription = {"method": "subscribe", "subscription": {"type": "bbo", "coin": normalized}}
        delay = self._stream_reconnect_initial_seconds
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                    await ws.send(json.dumps(subscription, separators=(",", ":")))
                    self.last_stream_error = None
                    delay = self._stream_reconnect_initial_seconds
                    async for raw in ws:
                        quote = self._parse_bbo_message(raw, normalized)
                        if quote is not None:
                            yield quote
                raise ConnectionError("Hyperliquid BBO stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_stream_error = f"{type(error).__name__}: {error}"[:300]
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._stream_reconnect_max_seconds)

    async def quantity_from_notional(self, coin: str, notional: Decimal, price: Decimal) -> Decimal:
        market = await self.market(coin)
        if (
            not isinstance(notional, Decimal)
            or not isinstance(price, Decimal)
            or not notional.is_finite()
            or not price.is_finite()
            or notional <= 0
            or price <= 0
        ):
            raise TradingError("notional amount and BBO price must be positive finite numbers")
        quantity = (notional / price).quantize(Decimal(1).scaleb(-market["size_decimals"]), rounding=ROUND_DOWN)
        if quantity <= 0:
            raise TradingError("quantity rounds to zero for Hyperliquid market")
        try:
            minimum = Decimal(str(market.get("min_quote_amount", "10")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise TradingError("invalid Hyperliquid minimum order value") from error
        if not minimum.is_finite() or minimum < 0:
            raise TradingError("invalid Hyperliquid minimum order value")
        if quantity * price < minimum:
            raise TradingError(f"converted order value is below Hyperliquid minimum ({minimum})")
        return quantity

    def _require_live(self) -> None:
        if not self.live_trading:
            raise TradingError("HYPERLIQUID_LIVE_TRADING=false: Hyperliquid real execution is locked")
        if not self.exchange:
            raise TradingError("Hyperliquid account/API wallet is not configured")

    @staticmethod
    def _validate_side(side: str) -> str:
        normalized = side.strip().lower()
        if normalized not in {"buy", "sell"}:
            raise TradingError("order side must be buy or sell")
        return normalized

    @staticmethod
    def _validate_client_id(client_id: str) -> str:
        normalized = client_id.strip()
        if not normalized or len(normalized) > 256:
            raise TradingError("invalid backend client order id")
        return normalized

    @classmethod
    def _cloid_hex(cls, client_id: str) -> str:
        """Map a durable backend ID to Hyperliquid's required 128-bit Cloid."""
        normalized = cls._validate_client_id(client_id)
        return "0x" + hashlib.sha256(f"hyperliquid-cloid:{normalized}".encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _require_positive_decimal(value: Decimal, field: str) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise TradingError(f"{field} must be a positive finite number")
        return value

    @staticmethod
    def _decimal_places(value: Decimal) -> int:
        return max(0, -value.normalize().as_tuple().exponent)

    @staticmethod
    def _significant_digits(value: Decimal) -> int:
        normalized = value.normalize()
        return len(normalized.as_tuple().digits)

    @classmethod
    def _validate_order_precision(cls, *, market: dict[str, Any], quantity: Decimal, price: Decimal) -> None:
        """Reject non-wire-compatible values rather than round a BBO price.

        Hyperliquid perps use the documented ``szDecimals`` lot rule and the
        price precision rule (five significant figures for non-integers; at
        most ``6 - szDecimals`` decimal places).  A valid exchange BBO is
        already aligned, so a mismatch is stale/corrupt input, not something
        this adapter should silently round into a different executable price.
        """
        quantity = cls._require_positive_decimal(quantity, "quantity")
        price = cls._require_positive_decimal(price, "BBO price")
        try:
            size_decimals = int(market["size_decimals"])
        except (KeyError, TypeError, ValueError) as error:
            raise TradingError("invalid Hyperliquid size precision metadata") from error
        if size_decimals < 0 or size_decimals > 6:
            raise TradingError("invalid Hyperliquid size precision metadata")
        if cls._decimal_places(quantity) > size_decimals:
            raise TradingError("quantity is not aligned to Hyperliquid lot size")
        max_price_decimals = 6 - size_decimals
        if cls._decimal_places(price) > max_price_decimals:
            raise TradingError("BBO price is not aligned to Hyperliquid tick precision")
        if price != price.to_integral_value() and cls._significant_digits(price) > 5:
            raise TradingError("BBO price exceeds Hyperliquid significant-figure precision")

    @staticmethod
    def _error_text(value: object) -> str:
        text = str(value).replace("\n", " ").strip()
        return text[:240] or "unspecified exchange error"

    @classmethod
    def _parse_order_result(cls, result: object, *, action: str) -> tuple[str | None, dict[str, Any]]:
        """Accept only explicit Hyperliquid resting/filled acknowledgements."""
        if not isinstance(result, dict):
            raise ExecutionUncertainError(
                f"Hyperliquid {action} returned no verifiable acknowledgement; reconcile before retrying"
            )
        if result.get("status") != "ok":
            raise TradingError(f"Hyperliquid rejected {action}: {cls._error_text(result.get('response', result))}")
        response = result.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if not isinstance(statuses, list) or len(statuses) != 1 or not isinstance(statuses[0], dict):
            raise ExecutionUncertainError(
                f"Hyperliquid {action} acknowledgement is incomplete; reconcile before retrying"
            )
        status = statuses[0]
        if "error" in status:
            raise TradingError(f"Hyperliquid rejected {action}: {cls._error_text(status['error'])}")
        details = status.get("resting") or status.get("filled")
        if not isinstance(details, dict):
            raise ExecutionUncertainError(
                f"Hyperliquid {action} acknowledgement has an unknown status; reconcile before retrying"
            )
        order_id = details.get("oid")
        return (str(order_id) if order_id is not None else None), status

    async def create_order(self, *, coin: str, side: str, quantity: Decimal, price: Decimal, reduce_only: bool, maker: bool, client_id: str) -> dict[str, Any]:
        self._require_live()
        normalized_side = self._validate_side(side)
        market = await self.market(coin)
        self._validate_order_precision(market=market, quantity=quantity, price=price)
        try:
            from hyperliquid.utils.types import Cloid
        except ImportError as error:
            raise TradingError("Hyperliquid SDK Cloid support is missing; install backend requirements") from error
        order_type = {"limit": {"tif": "Alo" if maker else "Ioc"}}
        # Hyperliquid accepts a fixed 16-byte hexadecimal Cloid. Derive it
        # deterministically from the persisted backend client ID so retries use
        # the exact same exchange-level identity without exposing a raw secret.
        cloid = Cloid.from_str(self._cloid_hex(client_id))
        try:
            result = await asyncio.to_thread(
                self.exchange.order,
                coin,
                normalized_side == "buy",
                float(quantity),
                float(price),
                order_type,
                reduce_only,
                cloid,
            )
        except asyncio.CancelledError:
            raise
        except (TypeError, ValueError) as error:
            raise TradingError(f"Hyperliquid rejected order locally: {self._error_text(error)}") from error
        except Exception as error:
            raise ExecutionUncertainError(
                "Hyperliquid submission outcome is unknown; query this client order id before retrying"
            ) from error
        order_id, _ = self._parse_order_result(result, action="order")
        return {
            "order_id": order_id,
            "client_order_id": self._validate_client_id(client_id),
            "exchange_client_order_id": str(cloid),
            "response": result,
        }

    async def cancel_order(self, coin: str, order_id: str) -> dict[str, Any]:
        self._require_live()
        try:
            return await asyncio.to_thread(self.exchange.cancel, coin, int(order_id))
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            raise TradingError("invalid Hyperliquid order id") from error
        except Exception as error:
            raise ExecutionUncertainError(
                "Hyperliquid cancel outcome is unknown; reconcile the exact exchange order before retrying"
            ) from error

    async def modify_order(self, *, coin: str, order_id: str, side: str, quantity: Decimal, price: Decimal, reduce_only: bool) -> dict[str, Any]:
        """Atomically modify the exact Hyperliquid maker order when supported."""
        self._require_live()
        normalized_side = self._validate_side(side)
        market = await self.market(coin)
        self._validate_order_precision(market=market, quantity=quantity, price=price)
        try:
            result = await asyncio.to_thread(
                self.exchange.modify_order,
                int(order_id),
                coin,
                normalized_side == "buy",
                float(quantity),
                float(price),
                {"limit": {"tif": "Alo"}},
                reduce_only,
            )
        except asyncio.CancelledError:
            raise
        except (TypeError, ValueError) as error:
            raise TradingError(f"Hyperliquid rejected modify locally: {self._error_text(error)}") from error
        except Exception as error:
            raise ExecutionUncertainError(
                "Hyperliquid modify outcome is unknown; reconcile the exact exchange order before retrying"
            ) from error
        new_order_id, _ = self._parse_order_result(result, action="modify")
        if new_order_id is None:
            raise ExecutionUncertainError(
                "Hyperliquid modify acknowledgement omitted the new order id; reconcile before retrying"
            )
        return {"order_id": new_order_id, "response": result}

    async def order_status_by_client_id(self, client_id: str) -> dict[str, Any]:
        """Read-only reconciliation hook for a prior possibly-unknown submit."""
        if not self.account_address:
            raise TradingError("Hyperliquid account address is not configured")
        try:
            result = await self._info(
                {"type": "orderStatus", "user": self.account_address, "oid": self._cloid_hex(client_id)}
            )
        except httpx.HTTPError as error:
            raise TradingError("Hyperliquid order-status lookup is unavailable") from error
        if not isinstance(result, dict):
            raise TradingError("Hyperliquid order-status lookup returned an invalid response")
        return result
