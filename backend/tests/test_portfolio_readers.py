from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from binance_gateway import BinanceGateway
from hyperliquid_gateway import HyperliquidGateway
from lighter_gateway import LighterGateway, Settings


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


def test_hyperliquid_portfolio_uses_read_only_clearinghouse_state() -> None:
    gateway = HyperliquidGateway()
    gateway.account_address = "0x" + "1" * 40

    async def fake_info(payload: dict[str, object]) -> dict[str, object]:
        assert payload == {"type": "clearinghouseState", "user": gateway.account_address}
        return {
            "marginSummary": {"accountValue": "125.5"},
            "withdrawable": "100.25",
            "assetPositions": [{"position": {
                "coin": "ETH", "szi": "0.5", "entryPx": "3000", "positionValue": "1600", "unrealizedPnl": "22.1",
            }}],
        }

    gateway._info = fake_info  # type: ignore[method-assign]
    snapshot = asyncio.run(gateway.portfolio_snapshot())
    assert snapshot["equity"] == "125.5"
    assert snapshot["available_margin"] == "100.25"
    assert snapshot["positions"][0]["side"] == "多"
    assert snapshot["positions"][0]["mark_price"] == "3200"
    asyncio.run(gateway.close())


def test_binance_portfolio_uses_signed_read_without_live_flag() -> None:
    gateway = BinanceGateway()

    async def fake_signed(method: str, path: str, params: dict[str, object], *, require_live: bool = True) -> dict[str, object]:
        assert (method, path, params, require_live) == ("GET", "/fapi/v3/account", {}, False)
        return {
            "totalMarginBalance": "525.5", "availableBalance": "440", "totalUnrealizedProfit": "5.5",
            "positions": [
                {"symbol": "BTCUSDT", "marginAsset": "USDT", "positionAmt": "0.01", "positionSide": "BOTH", "entryPrice": "100000", "markPrice": "100550", "unrealizedProfit": "5.5"},
                {"symbol": "ETHUSDT", "marginAsset": "USDT", "positionAmt": "0", "positionSide": "BOTH"},
            ],
        }

    gateway._signed = fake_signed  # type: ignore[method-assign]
    snapshot = asyncio.run(gateway.portfolio_snapshot())
    assert snapshot["equity"] == "525.5"
    assert snapshot["positions"] == [{
        "venue": "Binance USD-M", "symbol": "BTCUSDT", "side": "多", "quantity": "0.01",
        "entry_price": "100000", "mark_price": "100550", "unrealized_pnl": "5.5", "currency": "USDT",
    }]
    asyncio.run(gateway.close())


def test_lighter_portfolio_uses_account_index_and_keeps_currency_separate() -> None:
    settings = Settings(
        base_url="https://lighter.invalid", stream_url="wss://lighter.invalid", account_index=77,
        api_key_index=3, api_private_key="test-key", market_index=0, live_trading=False, max_reprices_per_minute=20,
    )
    gateway = LighterGateway(settings)

    async def fake_get(url: str, *, params: dict[str, str]) -> _Response:
        assert url == "https://lighter.invalid/api/v1/account"
        assert params == {"by": "index", "value": "77", "active_only": "true"}
        return _Response({"account": {
            "collateral": "321", "available_balance": "278", "positions": [{
                "symbol": "LIT-USD", "sign": -1, "position": "4", "avg_entry_price": "2.1", "position_value": "8", "unrealized_pnl": "-0.5",
            }],
        }})

    gateway.http.get = fake_get  # type: ignore[method-assign]
    snapshot = asyncio.run(gateway.portfolio_snapshot())
    assert snapshot["currency"] == "USDC"
    assert snapshot["equity"] == "321"
    assert snapshot["positions"][0]["side"] == "空"
    assert snapshot["positions"][0]["mark_price"] == "2"
    asyncio.run(gateway.close())
