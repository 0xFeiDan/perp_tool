"""The only module allowed to sign Lighter transactions.

The browser never receives the API private key. Market IOC orders are deliberately
implemented as LIMIT + IOC at the current BBO so they cannot consume deeper levels.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

import httpx
import lighter


class TradingError(RuntimeError):
    pass


class ExecutionUncertainError(TradingError):
    """A signed request may have reached the exchange but has no safe result.

    Callers must persist the client identity and reconcile the exchange before
    attempting another submission.  Retrying with a new client ID would risk a
    duplicate real order.
    """


@dataclass(frozen=True)
class Settings:
    base_url: str
    stream_url: str
    account_index: int | None
    api_key_index: int | None
    api_private_key: str | None
    market_index: int
    live_trading: bool
    max_reprices_per_minute: int

    @property
    def credentials_ready(self) -> bool:
        return self.account_index is not None and self.api_key_index is not None and bool(self.api_private_key)

    @classmethod
    def from_env(cls) -> "Settings":
        def optional_int(name: str) -> int | None:
            value = os.getenv(name, "").strip()
            return int(value) if value else None
        return cls(
            base_url=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/"),
            stream_url=os.getenv("LIGHTER_STREAM_URL", "wss://mainnet.zklighter.elliot.ai/stream"),
            account_index=optional_int("LIGHTER_ACCOUNT_INDEX"),
            api_key_index=optional_int("LIGHTER_API_KEY_INDEX"),
            api_private_key=os.getenv("LIGHTER_API_PRIVATE_KEY", "").strip() or None,
            market_index=int(os.getenv("LIGHTER_MARKET_INDEX", "0")),
            # Per-venue opt-in only.  Do not inherit a legacy global switch:
            # enabling Binance must never silently arm Lighter as well.
            live_trading=os.getenv("LIGHTER_LIVE_TRADING", "false").lower() == "true",
            max_reprices_per_minute=int(os.getenv("MAX_FOLLOW_REPRICES_PER_MINUTE", "20")),
        )


class LighterGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: lighter.SignerClient | None = None
        self.http = httpx.AsyncClient(timeout=10)
        self.price_decimals = 2
        self.size_decimals = 4
        self.market_metadata: dict[int, dict[str, Any]] = {}
        # Lighter accepts a uint48 client_order_index.  A timestamp modulo a
        # small range can collide after a process restart, which is unsafe for
        # reconciliation.  Keep a cryptographically random uint48 fallback;
        # persistent callers can supply their own deterministic client ID.
        self._client_index_owners: dict[int, str | None] = {}
        self._client_lock = asyncio.Lock()

    @property
    def live_trading(self) -> bool:
        """Match the common adapter contract without exposing mutable config."""
        return self.settings.live_trading

    @property
    def credentials_ready(self) -> bool:
        return self.settings.credentials_ready

    async def start(self) -> None:
        await self.refresh_market_metadata(self.settings.market_index)
        if self.settings.credentials_ready:
            self.client = lighter.SignerClient(
                url=self.settings.base_url,
                account_index=self.settings.account_index,
                api_private_keys={self.settings.api_key_index: self.settings.api_private_key},
            )
            problem = self.client.check_client()
            if problem:
                raise TradingError(f"Lighter API key check failed: {problem}")

    async def close(self) -> None:
        await self.http.aclose()
        if self.client and getattr(self.client, "api_client", None):
            await self.client.api_client.close()

    async def list_markets(self) -> list[dict[str, Any]]:
        response = await self.http.get(
            f"{self.settings.base_url}/api/v1/orderBookDetails",
            params={"filter": "perp"},
        )
        response.raise_for_status()
        payload = response.json()
        candidates = payload.get("order_book_details", payload.get("data", payload if isinstance(payload, list) else []))
        if isinstance(candidates, dict):
            candidates = candidates.get("order_book_details", candidates.get("markets", []))
        return [x for x in candidates if x.get("status") == "active" and x.get("market_type") == "perp"]

    async def refresh_market_metadata(self, market_index: int) -> dict[str, Any]:
        candidates = await self.list_markets()
        market = next((x for x in candidates if int(x.get("market_id", x.get("market_index", -1))) == market_index), None)
        if market is None:
            raise TradingError(f"active perpetual market {market_index} not found")
        self.market_metadata[market_index] = market
        return market

    async def get_market_metadata(self, market_index: int) -> dict[str, Any]:
        return self.market_metadata.get(market_index) or await self.refresh_market_metadata(market_index)

    @staticmethod
    def _validate_client_id(client_id: str) -> str:
        normalized = client_id.strip()
        if not normalized or len(normalized) > 256:
            raise TradingError("invalid backend client order id")
        return normalized

    async def next_client_order_id(self, client_id: str | None = None) -> int:
        """Return a uint48 identity, stable for a supplied backend client ID.

        The deterministic path is for the durable execution ledger: a retry
        uses exactly the same Lighter identity.  The random fallback preserves
        uniqueness for the current legacy caller without exposing a predictable
        sequence to the browser.
        """
        async with self._client_lock:
            if client_id is not None:
                normalized = self._validate_client_id(client_id)
                candidate = int.from_bytes(
                    hashlib.sha256(f"lighter-client-index:{normalized}".encode("utf-8")).digest()[:6],
                    byteorder="big",
                )
                # Zero is commonly reserved/missing in exchange integrations.
                candidate = candidate or 1
                existing = self._client_index_owners.get(candidate)
                if candidate in self._client_index_owners and existing != normalized:
                    # A 48-bit digest collision is very unlikely, but placing
                    # an order with the wrong durable identity is never safe.
                    raise TradingError("Lighter client-order-index collision; create a new backend intent")
                self._client_index_owners[candidate] = normalized
                return candidate

            upper_bound = (1 << 48) - 1
            while True:
                candidate = secrets.randbelow(upper_bound) + 1
                if candidate not in self._client_index_owners:
                    self._client_index_owners[candidate] = None
                    return candidate

    @staticmethod
    def _validate_side(side: str) -> str:
        normalized = side.strip().lower()
        if normalized not in {"buy", "sell"}:
            raise TradingError("order side must be buy or sell")
        return normalized

    def _to_units(self, value: Decimal, decimals: int, *, field: str) -> int:
        if decimals < 0:
            raise TradingError(f"invalid {field} precision from Lighter metadata")
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise TradingError(f"{field} must be a positive finite number")
        scaled = value * (Decimal(10) ** decimals)
        exact = scaled.to_integral_value()
        if scaled != exact:
            # Never silently round a BBO price.  In particular, rounding a
            # sell IOC down can cross more than the intended best bid level.
            raise TradingError(f"{field} is not aligned to this market's precision")
        scaled = exact
        if scaled <= 0:
            raise TradingError(f"{field} rounds to zero for this market")
        return int(scaled)

    def _validate_live(self) -> None:
        if not self.settings.live_trading:
            raise TradingError("LIGHTER_LIVE_TRADING=false: Lighter real execution is locked")
        if not self.client:
            raise TradingError("Lighter account/API key is not configured")

    async def quantity_from_notional(self, *, market_index: int, notional_usdc: Decimal, price: Decimal) -> Decimal:
        meta = await self.get_market_metadata(market_index)
        if not isinstance(notional_usdc, Decimal) or not notional_usdc.is_finite() or notional_usdc <= 0:
            raise TradingError("USDC amount must be a positive finite number")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise TradingError("BBO price must be a positive finite number")
        try:
            minimum_quote = Decimal(str(meta.get("min_quote_amount", "0")))
            minimum_base = Decimal(str(meta.get("min_base_amount", "0")))
            decimals = int(meta.get("supported_size_decimals", meta.get("size_decimals", 4)))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise TradingError("invalid Lighter market precision metadata") from error
        if minimum_quote < 0 or minimum_base < 0 or decimals < 0:
            raise TradingError("invalid Lighter market precision metadata")
        if notional_usdc < minimum_quote:
            raise TradingError(f"USDC amount is below this market's minimum ({minimum_quote})")
        step = Decimal(1).scaleb(-decimals)
        quantity = (notional_usdc / price).quantize(step, rounding=ROUND_DOWN)
        if quantity < minimum_base:
            raise TradingError(f"converted quantity is below this market's minimum ({minimum_base})")
        if quantity * price < minimum_quote:
            raise TradingError(f"converted order value is below this market's minimum ({minimum_quote})")
        return quantity

    async def create_bbo_order(
        self,
        *,
        market_index: int,
        side: str,
        quantity: Decimal,
        bbo_price: Decimal,
        reduce_only: bool,
        maker: bool,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit exactly one BBO-constrained limit order.

        ``maker=False`` always means LIMIT + IOC, not an unrestricted market
        order.  ``maker=True`` always means LIMIT + POST_ONLY.  The caller is
        responsible for selecting ask for buys / bid for sells in IOC mode,
        and bid for buys / ask for sells in maker mode.
        """
        self._validate_live()
        normalized_side = self._validate_side(side)
        meta = await self.get_market_metadata(market_index)
        price_decimals = int(meta.get("supported_price_decimals", meta.get("price_decimals", 2)))
        size_decimals = int(meta.get("supported_size_decimals", meta.get("size_decimals", 4)))
        client_order_index = await self.next_client_order_id(client_id)
        order_type = self.client.ORDER_TYPE_LIMIT
        tif = self.client.ORDER_TIME_IN_FORCE_POST_ONLY if maker else self.client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
        expiry = self.client.DEFAULT_28_DAY_ORDER_EXPIRY if maker else self.client.DEFAULT_IOC_EXPIRY
        try:
            tx, response, err = await self.client.create_order(
                market_index=market_index,
                client_order_index=client_order_index,
                base_amount=self._to_units(quantity, size_decimals, field="quantity"),
                price=self._to_units(bbo_price, price_decimals, field="BBO price"),
                is_ask=normalized_side == "sell",
                order_type=order_type,
                time_in_force=tif,
                reduce_only=reduce_only,
                order_expiry=expiry,
            )
        except asyncio.CancelledError:
            raise
        except TradingError:
            raise
        except Exception as error:
            raise ExecutionUncertainError(
                "Lighter submission outcome is unknown; reconcile this client order index before retrying"
            ) from error
        if err:
            raise TradingError(err)
        if response is None:
            raise ExecutionUncertainError(
                "Lighter returned no submission receipt; reconcile this client order index before retrying"
            )
        return {
            "client_order_index": client_order_index,
            "client_order_id": client_id or f"lighter:{client_order_index}",
            "tx_hash": getattr(response, "tx_hash", None),
            "response": str(response),
        }

    async def cancel_order(self, *, market_index: int, order_index: int) -> dict[str, Any]:
        if not self.client or not self.settings.live_trading:
            raise TradingError("real execution is locked")
        try:
            _, response, err = await self.client.cancel_order(market_index=market_index, order_index=order_index)
        except asyncio.CancelledError:
            raise
        except TradingError:
            raise
        except Exception as error:
            raise ExecutionUncertainError(
                "Lighter cancel outcome is unknown; reconcile the exact exchange order before retrying"
            ) from error
        if err:
            raise TradingError(err)
        return {"tx_hash": getattr(response, "tx_hash", None), "response": str(response)}

    async def modify_order(self, *, market_index: int, order_index: int, quantity: Decimal, price: Decimal) -> dict[str, Any]:
        # The price and quantity have already been normalized by the strategy.
        # Keep the same live-execution gate as order creation; this must not be
        # bypassed just because an existing maker order is being repriced.
        self._validate_live()
        if not self.client:
            raise TradingError("Lighter client unavailable")
        meta = await self.get_market_metadata(market_index)
        try:
            _, response, err = await self.client.modify_order(
                market_index=market_index,
                order_index=order_index,
                base_amount=self._to_units(
                    quantity,
                    int(meta.get("supported_size_decimals", meta.get("size_decimals", 4))),
                    field="quantity",
                ),
                price=self._to_units(
                    price,
                    int(meta.get("supported_price_decimals", meta.get("price_decimals", 2))),
                    field="BBO price",
                ),
            )
        except asyncio.CancelledError:
            raise
        except TradingError:
            raise
        except Exception as error:
            raise ExecutionUncertainError(
                "Lighter modify outcome is unknown; reconcile the exact exchange order before retrying"
            ) from error
        if err:
            raise TradingError(err)
        return {"tx_hash": getattr(response, "tx_hash", None), "response": str(response)}

    def create_auth_token(self) -> str | None:
        if not self.client or self.settings.api_key_index is None:
            return None
        token, error = self.client.create_auth_token_with_expiry(deadline=8 * 60 * 60, api_key_index=self.settings.api_key_index)
        if error:
            raise TradingError(error)
        return token
