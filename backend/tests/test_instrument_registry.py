from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from instrument_registry import InstrumentRegistry, InstrumentRegistryError


def test_lit_identifier_never_resolves_to_eth() -> None:
    registry = InstrumentRegistry()
    markets = registry.sync("lighter", [{"market_id": "120", "symbol": "LIT", "min_quote_amount": "10"}])
    lit = registry.resolve(venue="lighter", internal_instrument_id=markets[0]["internal_instrument_id"])
    assert lit.external_id == "120"
    assert lit.symbol == "LIT"


def test_symbol_overlap_is_scoped_to_venue() -> None:
    registry = InstrumentRegistry()
    lighter = registry.sync("lighter", [{"market_id": "3", "symbol": "BTC"}])[0]
    binance = registry.sync("binance", [{"market_id": "BTCUSDT", "symbol": "BTCUSDT"}])[0]
    assert lighter["internal_instrument_id"] != binance["internal_instrument_id"]
    with pytest.raises(InstrumentRegistryError, match="does not belong"):
        registry.resolve(venue="binance", internal_instrument_id=lighter["internal_instrument_id"])
