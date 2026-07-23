from __future__ import annotations

import asyncio
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from lighter_gateway import Settings, TradingError
from main import ExecuteRequest, StrategyService


class NoOrderLighter:
    live_trading = True
    credentials_ready = True

    def __init__(self) -> None:
        self.order_calls = 0

    async def start(self) -> None:
        return None

    async def quantity_from_notional(self, **_: object) -> Decimal:
        return Decimal("1")

    async def create_bbo_order(self, **_: object) -> dict[str, int]:
        self.order_calls += 1
        return {"client_order_index": 1}


def test_intent_cannot_execute_after_active_contract_changes() -> None:
    settings = Settings(
        base_url="https://example.invalid",
        stream_url="wss://example.invalid",
        account_index=None,
        api_key_index=None,
        api_private_key=None,
        market_index=0,
        live_trading=False,
        max_reprices_per_minute=20,
    )
    service = StrategyService(settings)
    fake_lighter = NoOrderLighter()
    service.lighter = fake_lighter  # type: ignore[assignment]
    service.trading_enabled = True
    service.venue = "lighter"
    service.market_id = "120"  # The backend has already moved to LIT.
    service.internal_instrument_id = "b" * 36
    service.market = {"symbol": "LIT"}
    service.bid = Decimal("10")
    service.ask = Decimal("11")
    service.bid_size = Decimal("1")
    service.ask_size = Decimal("1")
    service.quote_received_at = time.monotonic()

    stale = ExecuteRequest(
        venue="lighter",
        internal_instrument_id="a" * 36,
        side="buy",
        intent="open",
        mode="market",
        notional_amount=Decimal("10"),
        confirm_live=True,
    )

    with pytest.raises(TradingError, match="instrument does not match"):
        asyncio.run(service.execute(stale, expected_price=Decimal("11"), expected_quantity=Decimal("1")))
    assert fake_lighter.order_calls == 0


def test_durable_pre_submit_failure_happens_before_adapter_call() -> None:
    settings = Settings(
        base_url="https://example.invalid",
        stream_url="wss://example.invalid",
        account_index=None,
        api_key_index=None,
        api_private_key=None,
        market_index=0,
        live_trading=False,
        max_reprices_per_minute=20,
    )
    service = StrategyService(settings)
    fake_lighter = NoOrderLighter()
    service.lighter = fake_lighter  # type: ignore[assignment]
    service.trading_enabled = True
    service.venue = "lighter"
    service.market_id = "120"
    service.internal_instrument_id = "a" * 36
    service.market = {"symbol": "LIT"}
    service.bid = Decimal("10")
    service.ask = Decimal("11")
    service.bid_size = Decimal("1")
    service.ask_size = Decimal("1")
    service.quote_received_at = time.monotonic()

    execution = ExecuteRequest(
        venue="lighter",
        internal_instrument_id="a" * 36,
        side="buy",
        intent="open",
        mode="market",
        notional_amount=Decimal("10"),
        confirm_live=True,
    )

    async def storage_failure(_: Decimal, __: Decimal, ___: str) -> None:
        raise RuntimeError("database rejected the durable pre-submit record")

    with pytest.raises(RuntimeError, match="durable pre-submit"):
        asyncio.run(
            service.execute(
                execution,
                expected_price=Decimal("11"),
                expected_quantity=Decimal("1"),
                client_order_id="bbo-test-pre-submit",
                before_submit=storage_failure,
            )
        )
    assert fake_lighter.order_calls == 0
