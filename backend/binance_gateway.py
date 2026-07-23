"""Binance USD-M Futures adapter.  Only this backend signs REST requests."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

from lighter_gateway import ExecutionUncertainError, TradingError


class BinanceGateway:
    venue = "binance"
    _CLIENT_ORDER_ID = re.compile(r"^[.A-Za-z0-9_:/-]{1,36}$")

    def __init__(self) -> None:
        self.base_url = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")
        # Public market-data stream.  It deliberately does not use the API
        # key/secret and remains available while execution is globally locked.
        self.ws_url = os.getenv("BINANCE_FUTURES_WS_URL", "wss://fstream.binance.com/ws").rstrip("/")
        self.api_key = os.getenv("BINANCE_FUTURES_API_KEY", "").strip()
        self.api_secret = os.getenv("BINANCE_FUTURES_API_SECRET", "").strip()
        self.live_trading = os.getenv("BINANCE_LIVE_TRADING", "false").lower() == "true"
        self.http = httpx.AsyncClient(timeout=10)
        self.metadata: dict[str, dict[str, Any]] = {}
        self.last_stream_error: str | None = None
        self._stream_reconnect_initial_seconds = 1.0
        self._stream_reconnect_max_seconds = 15.0

    @property
    def credentials_ready(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def close(self) -> None:
        await self.http.aclose()

    async def markets(self) -> list[dict[str, Any]]:
        response = await self.http.get(f"{self.base_url}/fapi/v1/exchangeInfo")
        response.raise_for_status()
        result: list[dict[str, Any]] = []
        for item in response.json().get("symbols", []):
            if item.get("contractType") != "PERPETUAL" or item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT":
                continue
            filters = {entry["filterType"]: entry for entry in item.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            price = filters.get("PRICE_FILTER", {})
            notional = filters.get("MIN_NOTIONAL", {})
            market = {"market_id": item["symbol"], "symbol": item["symbol"], "min_quote_amount": str(notional.get("notional", "0")), "step_size": str(lot.get("stepSize", "0.001")), "tick_size": str(price.get("tickSize", "0.01"))}
            self.metadata[item["symbol"]] = market
            result.append(market)
        return sorted(result, key=lambda value: value["symbol"])

    async def market(self, symbol: str) -> dict[str, Any]:
        return self.metadata.get(symbol) or next((value for value in await self.markets() if value["market_id"] == symbol), None) or (_ for _ in ()).throw(TradingError("Binance market not found"))

    async def bbo(self, symbol: str) -> dict[str, Decimal]:
        """REST BBO fallback only.

        The strategy's primary path should consume :meth:`stream_bbo`; this
        method is intentionally retained for a short, safe recovery snapshot
        when no WebSocket quote has been received yet.
        """
        response = await self.http.get(f"{self.base_url}/fapi/v1/ticker/bookTicker", params={"symbol": symbol})
        response.raise_for_status(); item = response.json()
        quote = self._quote_from_values(item.get("bidPrice"), item.get("askPrice"), item.get("bidQty"), item.get("askQty"))
        if quote is None:
            raise TradingError("Binance returned an invalid BBO")
        return quote

    @staticmethod
    def _quote_from_values(
        bid: object,
        ask: object,
        bid_size: object,
        ask_size: object,
    ) -> dict[str, Decimal] | None:
        """Validate untrusted public feed fields before they reach pricing."""
        try:
            parsed = {
                "bid": Decimal(str(bid)),
                "ask": Decimal(str(ask)),
                "bid_size": Decimal(str(bid_size)),
                "ask_size": Decimal(str(ask_size)),
            }
        except (InvalidOperation, TypeError, ValueError):
            return None
        if (
            not all(value.is_finite() for value in parsed.values())
            or parsed["bid"] <= 0
            or parsed["ask"] <= 0
            or parsed["bid"] >= parsed["ask"]
            or parsed["bid_size"] < 0
            or parsed["ask_size"] < 0
        ):
            return None
        return parsed

    @classmethod
    def _parse_book_ticker(cls, message: str | bytes | dict[str, Any], expected_symbol: str) -> dict[str, Decimal] | None:
        """Parse one public ``<symbol>@bookTicker`` payload defensively."""
        try:
            payload = json.loads(message.decode("utf-8") if isinstance(message, bytes) else message) if not isinstance(message, dict) else message
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None
        # Supporting a combined-stream wrapper costs nothing and keeps this
        # parser useful should the transport later multiplex subscriptions.
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            return None
        if str(payload.get("s", "")).upper() != expected_symbol.upper():
            return None
        event_type = payload.get("e")
        if event_type not in {None, "bookTicker"}:
            return None
        return cls._quote_from_values(payload.get("b"), payload.get("a"), payload.get("B"), payload.get("A"))

    @staticmethod
    def _book_ticker_url(ws_url: str, symbol: str) -> str:
        normalized = symbol.upper()
        # Symbols are normally resolved from server-owned market metadata, but
        # still keep the WebSocket URL free of path/control-character input.
        if not re.fullmatch(r"[A-Z0-9]{2,32}", normalized):
            raise TradingError("invalid Binance futures symbol for BBO stream")
        return f"{ws_url.rstrip('/')}/{normalized.lower()}@bookTicker"

    async def stream_bbo(self, symbol: str) -> AsyncIterator[dict[str, Decimal]]:
        """Yield validated real-time USD-M best bid/ask quotes forever.

        The public stream is intentionally unauthenticated.  On an unexpected
        disconnect it reconnects with bounded exponential backoff; cancellation
        is propagated immediately so the caller can stop it during a market
        switch.  No order, private-account, or credential operation exists on
        this code path.
        """
        normalized = symbol.upper()
        url = self._book_ticker_url(self.ws_url, normalized)
        delay = self._stream_reconnect_initial_seconds
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                    self.last_stream_error = None
                    delay = self._stream_reconnect_initial_seconds
                    async for raw in ws:
                        quote = self._parse_book_ticker(raw, normalized)
                        if quote is not None:
                            yield quote
                # A clean EOF is still not a usable live feed; reconnect it.
                raise ConnectionError("Binance BBO stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_stream_error = f"{type(error).__name__}: {error}"[:300]
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._stream_reconnect_max_seconds)

    @staticmethod
    def _floor(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    async def quantity_from_notional(self, symbol: str, notional: Decimal, price: Decimal) -> Decimal:
        market = await self.market(symbol)
        if not isinstance(notional, Decimal) or not notional.is_finite() or notional <= 0:
            raise TradingError("notional amount must be a positive finite number")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise TradingError("BBO price must be a positive finite number")
        minimum = self._positive_decimal(market["min_quote_amount"], "Binance minimum notional")
        step = self._positive_decimal(market["step_size"], "Binance lot size")
        if notional < minimum:
            raise TradingError(f"USDT amount is below Binance minimum ({minimum})")
        quantity = self._floor(notional / price, step)
        if quantity <= 0:
            raise TradingError("quantity rounds to zero for Binance market")
        if quantity * price < minimum:
            raise TradingError(f"converted order value is below Binance minimum ({minimum})")
        return quantity

    @staticmethod
    def _positive_decimal(value: object, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise TradingError(f"invalid {field}") from error
        if not parsed.is_finite() or parsed <= 0:
            raise TradingError(f"invalid {field}")
        return parsed

    @staticmethod
    def _validate_side(side: str) -> str:
        normalized = side.strip().lower()
        if normalized not in {"buy", "sell"}:
            raise TradingError("order side must be buy or sell")
        return normalized

    @classmethod
    def _validate_client_id(cls, client_id: str) -> str:
        normalized = client_id.strip()
        if not cls._CLIENT_ORDER_ID.fullmatch(normalized):
            raise TradingError("invalid Binance client order id")
        return normalized

    @classmethod
    def _validate_order_precision(
        cls,
        *,
        market: dict[str, Any],
        quantity: Decimal,
        price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
            raise TradingError("quantity must be a positive finite number")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise TradingError("BBO price must be a positive finite number")
        step = cls._positive_decimal(market["step_size"], "Binance lot size")
        tick = cls._positive_decimal(market["tick_size"], "Binance tick size")
        if cls._floor(quantity, step) != quantity:
            raise TradingError("quantity is not aligned to Binance lot size")
        if cls._floor(price, tick) != price:
            # Do not silently floor the price: a sell IOC below bid1 could
            # consume deeper bids and violate the strategy's BBO-only rule.
            raise TradingError("BBO price is not aligned to Binance tick size")
        return step, tick

    def _require_live(self) -> None:
        if not self.live_trading:
            raise TradingError("BINANCE_LIVE_TRADING=false: Binance real execution is locked")
        self._require_credentials()

    def _require_credentials(self) -> None:
        if not self.credentials_ready:
            raise TradingError("Binance Futures API key/secret is not configured")

    async def _signed(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        require_live: bool = True,
    ) -> dict[str, Any]:
        if require_live:
            self._require_live()
        else:
            self._require_credentials()
        signed = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(signed, doseq=True)
        signed["signature"] = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            response = await self.http.request(
                method,
                f"{self.base_url}{path}",
                params=signed,
                headers={"X-MBX-APIKEY": self.api_key},
            )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as error:
            if require_live:
                raise ExecutionUncertainError(
                    "Binance submission outcome is unknown; query the client order id before retrying"
                ) from error
            raise TradingError("Binance signed read request is unavailable") from error
        if response.is_error:
            message = response.text.replace("\n", " ")[:300]
            if require_live and response.status_code >= 500:
                raise ExecutionUncertainError(
                    "Binance returned a server error; query the client order id before retrying"
                )
            raise TradingError(f"Binance rejected: {message}")
        try:
            result = response.json()
        except ValueError as error:
            if require_live:
                raise ExecutionUncertainError(
                    "Binance returned an invalid submission response; query the client order id before retrying"
                ) from error
            raise TradingError("Binance signed read request returned invalid JSON") from error
        if not isinstance(result, dict):
            if require_live:
                raise ExecutionUncertainError(
                    "Binance returned an incomplete submission response; query the client order id before retrying"
                )
            raise TradingError("Binance signed read request returned an invalid response")
        if isinstance(result.get("code"), int) and result["code"] < 0:
            raise TradingError(f"Binance rejected: {str(result.get('msg', 'unspecified exchange error'))[:240]}")
        return result

    async def _is_hedge_mode(self) -> bool:
        """Read the account's real position mode before encoding a close order."""
        result = await self._signed("GET", "/fapi/v1/positionSide/dual", {}, require_live=False)
        value = result.get("dualSidePosition")
        if not isinstance(value, bool):
            raise TradingError("Binance position-mode query returned an invalid response")
        return value

    @staticmethod
    def _position_side_for_hedge_mode(*, side: str, reduce_only: bool) -> str:
        # In Hedge Mode Binance rejects reduceOnly.  The requested semantic is
        # still unambiguous: open buy/sell maps to LONG/SHORT; close reverses.
        return {
            ("buy", False): "LONG",
            ("sell", False): "SHORT",
            ("buy", True): "SHORT",
            ("sell", True): "LONG",
        }[(side, reduce_only)]

    async def create_order(self, *, symbol: str, side: str, quantity: Decimal, price: Decimal, reduce_only: bool, maker: bool, client_id: str) -> dict[str, Any]:
        # Do not even issue the private position-mode read until this venue is
        # explicitly armed for execution.  The control plane also checks this,
        # but the adapter must remain safe when called independently.
        self._require_live()
        market = await self.market(symbol)
        normalized_side = self._validate_side(side)
        self._validate_order_precision(market=market, quantity=quantity, price=price)
        normalized_client_id = self._validate_client_id(client_id)
        hedge_mode = await self._is_hedge_mode()
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": normalized_side.upper(),
            "type": "LIMIT",
            "quantity": format(quantity, "f"),
            "price": format(price, "f"),
            "timeInForce": "GTX" if maker else "IOC",
            "newClientOrderId": normalized_client_id,
            # RESULT gives the final IOC result instead of a bare ACK so the
            # ledger can distinguish known expiry/partial fill from uncertainty.
            "newOrderRespType": "RESULT",
        }
        if hedge_mode:
            payload["positionSide"] = self._position_side_for_hedge_mode(
                side=normalized_side,
                reduce_only=reduce_only,
            )
        else:
            payload["reduceOnly"] = "true" if reduce_only else "false"
        result = await self._signed("POST", "/fapi/v1/order", payload)
        order_id = result.get("orderId")
        if order_id is None:
            raise ExecutionUncertainError(
                "Binance acknowledged no exchange order id; query the client order id before retrying"
            )
        return {
            "order_id": str(order_id),
            "client_order_id": normalized_client_id,
            "status": result.get("status"),
            "response": result,
        }

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return await self._signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    async def order_by_client_id(self, symbol: str, client_id: str) -> dict[str, Any]:
        """Read-only reconciliation hook for an uncertain prior submission."""
        return await self._signed(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": self._validate_client_id(client_id)},
            require_live=False,
        )
