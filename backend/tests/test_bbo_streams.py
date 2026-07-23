from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from binance_gateway import BinanceGateway
from hyperliquid_gateway import HyperliquidGateway
from lighter_gateway import TradingError


class FakeWebSocket:
    """Minimal async socket used to verify public-stream behavior offline."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = iter(messages)
        self.sent: list[str] = []

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        try:
            return next(self.messages)
        except StopIteration as error:
            raise StopAsyncIteration from error


def test_binance_book_ticker_parser_returns_decimals() -> None:
    quote = BinanceGateway._parse_book_ticker(
        '{"e":"bookTicker","s":"BTCUSDT","b":"100.10","B":"2.5","a":"100.20","A":"3.75"}',
        "BTCUSDT",
    )
    assert quote == {
        "bid": Decimal("100.10"),
        "ask": Decimal("100.20"),
        "bid_size": Decimal("2.5"),
        "ask_size": Decimal("3.75"),
    }


def test_binance_parser_rejects_wrong_or_crossed_messages() -> None:
    assert BinanceGateway._parse_book_ticker({"s": "ETHUSDT", "b": "1", "B": "1", "a": "2", "A": "1"}, "BTCUSDT") is None
    assert BinanceGateway._parse_book_ticker({"s": "BTCUSDT", "b": "2", "B": "1", "a": "2", "A": "1"}, "BTCUSDT") is None
    with pytest.raises(TradingError, match="invalid Binance"):
        BinanceGateway._book_ticker_url("wss://example.invalid/ws", "BTC/USDT")


def test_hyperliquid_bbo_and_l2book_parsers_return_decimals() -> None:
    bbo = HyperliquidGateway._parse_bbo_message(
        {
            "channel": "bbo",
            "data": {"coin": "ETH", "bbo": [{"px": "2000.1", "sz": "1.2"}, {"px": "2000.2", "sz": "2.3"}]},
        },
        "ETH",
    )
    snapshot = HyperliquidGateway._parse_bbo_message(
        {
            "channel": "l2Book",
            "data": {"coin": "ETH", "levels": [[{"px": "2000.1", "sz": "1.2"}], [{"px": "2000.2", "sz": "2.3"}]]},
        },
        "ETH",
    )
    expected = {"bid": Decimal("2000.1"), "ask": Decimal("2000.2"), "bid_size": Decimal("1.2"), "ask_size": Decimal("2.3")}
    assert bbo == expected
    assert snapshot == expected


def test_hyperliquid_parser_rejects_ack_wrong_coin_and_empty_side() -> None:
    assert HyperliquidGateway._parse_bbo_message({"channel": "subscriptionResponse", "data": {}}, "ETH") is None
    assert HyperliquidGateway._parse_bbo_message({"channel": "bbo", "data": {"coin": "BTC", "bbo": [{"px": "1", "sz": "1"}, {"px": "2", "sz": "1"}]}}, "ETH") is None
    assert HyperliquidGateway._parse_bbo_message({"channel": "bbo", "data": {"coin": "ETH", "bbo": [None, {"px": "2", "sz": "1"}]}}, "ETH") is None


def test_binance_stream_yields_a_public_bbo_without_api_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = FakeWebSocket(['{"e":"bookTicker","s":"BTCUSDT","b":"10","B":"1","a":"11","A":"2"}'])
    monkeypatch.setattr("binance_gateway.websockets.connect", lambda *_args, **_kwargs: socket)
    gateway = BinanceGateway()

    async def collect_one() -> dict[str, Decimal]:
        stream = gateway.stream_bbo("BTCUSDT")
        try:
            return await anext(stream)
        finally:
            await stream.aclose()
            await gateway.close()

    assert asyncio.run(collect_one())["ask"] == Decimal("11")


def test_binance_stream_recovers_after_a_closed_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    sockets = iter([
        FakeWebSocket([]),
        FakeWebSocket(['{"e":"bookTicker","s":"BTCUSDT","b":"10","B":"1","a":"11","A":"2"}']),
    ])
    monkeypatch.setattr("binance_gateway.websockets.connect", lambda *_args, **_kwargs: next(sockets))
    gateway = BinanceGateway()
    # Keep the reconnection test offline and instantaneous; production still
    # uses the gateway's bounded one-second-to-fifteen-second backoff.
    gateway._stream_reconnect_initial_seconds = 0

    async def collect_after_reconnect() -> dict[str, Decimal]:
        stream = gateway.stream_bbo("BTCUSDT")
        try:
            return await anext(stream)
        finally:
            await stream.aclose()
            await gateway.close()

    assert asyncio.run(collect_after_reconnect())["bid"] == Decimal("10")


def test_hyperliquid_stream_uses_public_bbo_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = FakeWebSocket([json.dumps({"channel": "bbo", "data": {"coin": "ETH", "bbo": [{"px": "10", "sz": "1"}, {"px": "11", "sz": "2"}]}})])
    monkeypatch.setattr("hyperliquid_gateway.websockets.connect", lambda *_args, **_kwargs: socket)
    gateway = HyperliquidGateway()

    async def collect_one() -> dict[str, Decimal]:
        stream = gateway.stream_bbo("ETH")
        try:
            return await anext(stream)
        finally:
            await stream.aclose()
            await gateway.close()

    assert asyncio.run(collect_one())["bid"] == Decimal("10")
    assert json.loads(socket.sent[0]) == {"method": "subscribe", "subscription": {"type": "bbo", "coin": "ETH"}}
