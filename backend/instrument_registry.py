"""Trusted, venue-scoped instrument identifiers.

The browser receives an opaque internal identifier and never supplies an
exchange symbol for order execution.  The registry is deliberately keyed by
both venue and external identifier, so ``BTC`` on two venues can never resolve
to the wrong adapter.  A production repository can persist these same fields
without changing the control-plane contract.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


class InstrumentRegistryError(RuntimeError):
    pass


_NAMESPACE = uuid.UUID("d33e9c8d-ae4d-4a16-af1c-3c271a05a2aa")


@dataclass(frozen=True)
class Instrument:
    internal_instrument_id: str
    venue: str
    external_id: str
    symbol: str
    base_asset: str | None
    quote_asset: str | None
    settle_asset: str | None
    min_notional: str | None
    quantity_step: str | None
    price_tick: str | None
    status: str
    metadata: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {
            "internal_instrument_id": self.internal_instrument_id,
            "market_id": self.external_id,
            "symbol": self.symbol,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "settle_asset": self.settle_asset,
            "min_quote_amount": self.min_notional,
            "step_size": self.quantity_step,
            "tick_size": self.price_tick,
            "status": self.status,
            "market_scope": self.metadata.get("market_scope"),
        }


class InstrumentRegistry:
    def __init__(self) -> None:
        self._by_internal_id: dict[str, Instrument] = {}
        self._by_venue_external: dict[tuple[str, str], str] = {}

    @staticmethod
    def _make_id(venue: str, external_id: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{venue}:{external_id}"))

    def sync(self, venue: str, raw_markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Register current adapter metadata and return public trusted records."""
        result: list[dict[str, Any]] = []
        for raw in raw_markets:
            external_id = str(raw.get("market_id", "")).strip()
            symbol = str(raw.get("symbol", "")).strip()
            if not external_id or not symbol:
                continue
            internal_id = self._make_id(venue, external_id)
            record = Instrument(
                internal_instrument_id=internal_id,
                venue=venue,
                external_id=external_id,
                symbol=symbol,
                base_asset=str(raw.get("base_asset") or symbol.removesuffix("USDT").removesuffix("USD") or symbol),
                quote_asset=str(raw.get("quote_asset") or ("USDT" if venue == "binance" else "USDC")),
                settle_asset=str(raw.get("settle_asset") or ("USDT" if venue == "binance" else "USDC")),
                min_notional=str(raw.get("min_quote_amount")) if raw.get("min_quote_amount") is not None else None,
                quantity_step=str(raw.get("step_size")) if raw.get("step_size") is not None else None,
                price_tick=str(raw.get("tick_size")) if raw.get("tick_size") is not None else None,
                status=str(raw.get("status") or "active"),
                metadata={key: value for key, value in raw.items() if key not in {"market_id", "symbol"}},
            )
            self._by_internal_id[internal_id] = record
            self._by_venue_external[(venue, external_id)] = internal_id
            result.append(record.public())
        return sorted(result, key=lambda item: str(item["symbol"]))

    def resolve(self, *, venue: str, internal_instrument_id: str) -> Instrument:
        record = self._by_internal_id.get(internal_instrument_id)
        if record is None:
            raise InstrumentRegistryError("instrument is unknown or metadata has not been loaded")
        if record.venue != venue:
            raise InstrumentRegistryError("instrument does not belong to the selected exchange")
        if record.status.lower() not in {"active", "trading"}:
            raise InstrumentRegistryError("instrument is not currently tradable")
        return record

    def internal_id_for(self, *, venue: str, external_id: str) -> str | None:
        return self._by_venue_external.get((venue, str(external_id)))
