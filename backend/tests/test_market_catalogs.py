from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from binance_gateway import BinanceGateway
from hyperliquid_gateway import HyperliquidGateway


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


def test_hyperliquid_catalog_is_primary_usdc_perps_and_skips_delisted() -> None:
    gateway = HyperliquidGateway()

    async def fake_info(_: dict[str, object]) -> dict[str, object]:
        return {"universe": [
            {"name": "BTC", "szDecimals": 5},
            {"name": "OLD", "szDecimals": 2, "isDelisted": True},
        ]}

    gateway._info = fake_info  # type: ignore[method-assign]
    markets = asyncio.run(gateway.markets())
    assert markets == [{
        "market_id": "BTC", "symbol": "BTC", "base_asset": "BTC",
        "quote_asset": "USDC", "settle_asset": "USDC", "market_scope": "USDC 永续",
        "asset_index": 0, "size_decimals": 5, "min_quote_amount": "10",
    }]
    asyncio.run(gateway.close())


def test_binance_catalog_only_returns_trading_usdt_perpetuals() -> None:
    gateway = BinanceGateway()
    async def fake_get(*_args: object, **_kwargs: object) -> _Response:
        return _Response({"symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001"}, {"filterType": "PRICE_FILTER", "tickSize": "0.10"}, {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]},
            {"symbol": "BTCUSDC", "baseAsset": "BTC", "quoteAsset": "USDC", "contractType": "PERPETUAL", "status": "TRADING", "filters": []},
            {"symbol": "BTCUSDT_240628", "baseAsset": "BTC", "quoteAsset": "USDT", "contractType": "CURRENT_QUARTER", "status": "TRADING", "filters": []},
            {"symbol": "CLOSEDUSDT", "baseAsset": "CLOSED", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "BREAK", "filters": []},
        ]})

    gateway.http.get = fake_get  # type: ignore[method-assign]
    markets = asyncio.run(gateway.markets())
    assert [market["market_id"] for market in markets] == ["BTCUSDT"]
    assert markets[0]["market_scope"] == "USDT 永续"
    asyncio.run(gateway.close())
