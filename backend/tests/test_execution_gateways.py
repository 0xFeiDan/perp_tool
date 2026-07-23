from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from binance_gateway import BinanceGateway
from hyperliquid_gateway import HyperliquidGateway
from lighter_gateway import ExecutionUncertainError, LighterGateway, Settings, TradingError


class FakeLighterClient:
    ORDER_TYPE_LIMIT = 0
    ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
    ORDER_TIME_IN_FORCE_POST_ONLY = 2
    DEFAULT_IOC_EXPIRY = 1
    DEFAULT_28_DAY_ORDER_EXPIRY = 2

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_order(self, **kwargs: object) -> tuple[object, object, None]:
        self.calls.append(kwargs)
        return object(), SimpleNamespace(tx_hash="0xreceipt"), None


def _lighter_gateway() -> LighterGateway:
    settings = Settings(
        base_url="https://example.invalid",
        stream_url="wss://example.invalid",
        account_index=1,
        api_key_index=1,
        api_private_key="not-a-real-key",
        market_index=1,
        live_trading=True,
        max_reprices_per_minute=20,
    )
    gateway = LighterGateway(settings)
    gateway.market_metadata[1] = {"supported_price_decimals": 2, "supported_size_decimals": 3}
    gateway.client = FakeLighterClient()  # type: ignore[assignment]
    return gateway


def test_lighter_ioc_is_exact_bbo_and_client_id_is_stable() -> None:
    gateway = _lighter_gateway()

    async def scenario() -> None:
        first = await gateway.next_client_order_id("intent-123")
        assert await gateway.next_client_order_id("intent-123") == first
        result = await gateway.create_bbo_order(
            market_index=1,
            side="buy",
            quantity=Decimal("1.234"),
            bbo_price=Decimal("100.12"),
            reduce_only=False,
            maker=False,
            client_id="intent-123",
        )
        call = gateway.client.calls[-1]  # type: ignore[union-attr]
        assert call["is_ask"] is False
        assert call["time_in_force"] == FakeLighterClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
        assert call["price"] == 10012
        assert result["client_order_index"] == first
        with pytest.raises(TradingError, match="BBO price is not aligned"):
            await gateway.create_bbo_order(
                market_index=1,
                side="sell",
                quantity=Decimal("1.234"),
                bbo_price=Decimal("100.123"),
                reduce_only=False,
                maker=False,
            )
        await gateway.close()

    asyncio.run(scenario())


class FakeHyperExchange:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[object, ...]] = []

    def order(self, *args: object) -> object:
        self.calls.append(args)
        return self.response


def test_hyperliquid_rejection_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERLIQUID_LIVE_TRADING", "true")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("HYPERLIQUID_API_WALLET_PRIVATE_KEY", "not-a-real-key")
    gateway = HyperliquidGateway()
    exchange = FakeHyperExchange({"status": "ok", "response": {"data": {"statuses": [{"error": "IocCancel"}]}}})
    gateway.exchange = exchange
    gateway.metadata["ETH"] = {"market_id": "ETH", "symbol": "ETH", "size_decimals": 3, "min_quote_amount": "10"}

    async def scenario() -> None:
        with pytest.raises(TradingError, match="IocCancel"):
            await gateway.create_order(
                coin="ETH",
                side="buy",
                quantity=Decimal("1.000"),
                price=Decimal("100.12"),
                reduce_only=False,
                maker=False,
                client_id="intent-456",
            )
        assert len(exchange.calls) == 1
        with pytest.raises(TradingError, match="tick precision"):
            await gateway.create_order(
                coin="ETH",
                side="sell",
                quantity=Decimal("1.000"),
                price=Decimal("100.1234"),
                reduce_only=False,
                maker=True,
                client_id="intent-457",
            )
        await gateway.close()

    asyncio.run(scenario())


def test_binance_order_payload_uses_ioc_or_gtx_without_price_flooring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "fake-key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "fake-secret")
    gateway = BinanceGateway()
    gateway.metadata["BTCUSDT"] = {
        "market_id": "BTCUSDT",
        "symbol": "BTCUSDT",
        "min_quote_amount": "10",
        "step_size": "0.001",
        "tick_size": "0.10",
    }
    calls: list[tuple[str, str, dict[str, object], bool]] = []

    async def fake_signed(
        method: str,
        path: str,
        params: dict[str, object],
        *,
        require_live: bool = True,
    ) -> dict[str, object]:
        calls.append((method, path, params, require_live))
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": True}
        return {"orderId": 42, "status": "NEW"}

    gateway._signed = fake_signed  # type: ignore[method-assign]

    async def scenario() -> None:
        result = await gateway.create_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=Decimal("0.100"),
            price=Decimal("100.10"),
            reduce_only=True,
            maker=False,
            client_id="bbo-intent-789",
        )
        payload = calls[-1][2]
        assert payload["timeInForce"] == "IOC"
        assert payload["price"] == "100.10"
        assert payload["positionSide"] == "SHORT"
        assert "reduceOnly" not in payload
        assert result["order_id"] == "42"
        with pytest.raises(TradingError, match="tick size"):
            await gateway.create_order(
                symbol="BTCUSDT",
                side="sell",
                quantity=Decimal("0.100"),
                price=Decimal("100.09"),
                reduce_only=False,
                maker=True,
                client_id="bbo-intent-790",
            )
        await gateway.close()

    asyncio.run(scenario())


def test_binance_transport_failure_is_explicitly_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "fake-key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "fake-secret")
    gateway = BinanceGateway()

    async def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    async def scenario() -> None:
        await gateway.http.aclose()
        gateway.http = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        with pytest.raises(ExecutionUncertainError, match="outcome is unknown"):
            await gateway._signed("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
        await gateway.close()

    asyncio.run(scenario())
