"""Offline tests for the future persistence foundation.

They deliberately use no .env values, no network calls, and no exchange SDKs.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from domain_models import (  # noqa: E402
    AccountMode,
    AuditEvent,
    DomainValidationError,
    ExchangeAccount,
    ExecutionMode,
    FollowStrategy,
    Instrument,
    InstrumentType,
    Order,
    OrderIntent,
    Side,
    TradeIntent,
    User,
    Venue,
    create_confirmation_token,
    new_id,
    utc_now,
)
from persistence import (  # noqa: E402
    SQLALCHEMY_AVAILABLE,
    PersistenceDependencyError,
    create_database_engine,
    initialize_schema,
    schema_table_names,
)


def request_fingerprint() -> str:
    return hashlib.sha256(b"offline-test-request").hexdigest()


class DomainModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = User(id=new_id(), username="operator_1", email="operator@example.test")
        self.account = ExchangeAccount(
            id=new_id(),
            user_id=self.user.id,
            venue=Venue.BINANCE,
            label="primary-futures",
            mode=AccountMode.LIVE,
            credential_reference="env:BINANCE_FUTURES_PRIMARY",
        )
        self.instrument = Instrument(
            id=new_id(),
            venue=Venue.BINANCE,
            external_id="BTCUSDT",
            symbol="BTCUSDT",
            instrument_type=InstrumentType.PERPETUAL,
            min_notional=Decimal("5"),
            quantity_step=Decimal("0.001"),
            price_tick=Decimal("0.1"),
        )

    def test_account_never_accepts_raw_credentials_and_mt5_is_read_only(self) -> None:
        with self.assertRaises(DomainValidationError):
            ExchangeAccount(
                id=new_id(),
                user_id=self.user.id,
                venue=Venue.BINANCE,
                label="unsafe",
                mode=AccountMode.LIVE,
                credential_reference="this-is-not-a-reference",
            )
        with self.assertRaises(DomainValidationError):
            ExchangeAccount(
                id=new_id(),
                user_id=self.user.id,
                venue=Venue.MT5,
                label="mt5-live-forbidden",
                mode=AccountMode.LIVE,
                credential_reference="env:MT5_PASSWORD",
            )
        mt5 = ExchangeAccount(
            id=new_id(),
            user_id=self.user.id,
            venue=Venue.MT5,
            label="mt5-investor",
            mode=AccountMode.READ_ONLY,
        )
        self.assertFalse(mt5.can_trade)

    def test_instrument_key_is_venue_scoped(self) -> None:
        self.assertEqual(self.instrument.canonical_key, "binance:btcusdt")
        with self.assertRaises(DomainValidationError):
            OrderIntent.create(
                user_id=self.user.id,
                exchange_account_id=self.account.id,
                instrument_id=self.instrument.id,
                venue=Venue.BINANCE,
                instrument_key="lighter:120",
                side=Side.BUY,
                intent=TradeIntent.OPEN,
                mode=ExecutionMode.MARKET,
                notional=Decimal("100"),
                confirmation_token=create_confirmation_token(),
                request_fingerprint=request_fingerprint(),
            )

    def test_order_intent_is_short_lived_one_time_confirmation(self) -> None:
        now = utc_now()
        token = create_confirmation_token()
        intent = OrderIntent.create(
            user_id=self.user.id,
            exchange_account_id=self.account.id,
            instrument_id=self.instrument.id,
            venue=Venue.BINANCE,
            instrument_key=self.instrument.canonical_key,
            side=Side.BUY,
            intent=TradeIntent.OPEN,
            mode=ExecutionMode.MARKET,
            notional=Decimal("100"),
            confirmation_token=token,
            request_fingerprint=request_fingerprint(),
            now=now,
        )
        confirmed = intent.confirm(token, now=now + timedelta(seconds=1))
        consumed = confirmed.consume(now=now + timedelta(seconds=2))
        self.assertEqual(consumed.status.value, "consumed")
        with self.assertRaises(DomainValidationError):
            consumed.confirm(token, now=now + timedelta(seconds=3))
        with self.assertRaises(DomainValidationError):
            OrderIntent.create(
                user_id=self.user.id,
                exchange_account_id=self.account.id,
                instrument_id=self.instrument.id,
                venue=Venue.BINANCE,
                instrument_key=self.instrument.canonical_key,
                side=Side.BUY,
                intent=TradeIntent.OPEN,
                mode=ExecutionMode.MARKET,
                notional=Decimal("100"),
                confirmation_token=create_confirmation_token(),
                request_fingerprint=request_fingerprint(),
                now=now,
                expires_in_seconds=31,
            )

    def test_close_order_and_follow_are_always_reduce_only(self) -> None:
        token = create_confirmation_token()
        intent = OrderIntent.create(
            user_id=self.user.id,
            exchange_account_id=self.account.id,
            instrument_id=self.instrument.id,
            venue=Venue.BINANCE,
            instrument_key=self.instrument.canonical_key,
            side=Side.SELL,
            intent=TradeIntent.CLOSE,
            mode=ExecutionMode.MAKER,
            notional=Decimal("100"),
            confirmation_token=token,
            request_fingerprint=request_fingerprint(),
        )
        with self.assertRaises(DomainValidationError):
            Order(
                id=new_id(),
                order_intent_id=intent.id,
                exchange_account_id=self.account.id,
                instrument_id=self.instrument.id,
                venue=Venue.BINANCE,
                instrument_key=self.instrument.canonical_key,
                client_order_id="offline-close-1",
                side=Side.SELL,
                intent=TradeIntent.CLOSE,
                mode=ExecutionMode.MAKER,
                quantity=Decimal("0.01"),
                limit_price=Decimal("100000"),
                reduce_only=False,
            )
        follow = FollowStrategy(
            id=new_id(),
            user_id=self.user.id,
            exchange_account_id=self.account.id,
            instrument_id=self.instrument.id,
            venue=Venue.BINANCE,
            instrument_key=self.instrument.canonical_key,
            side=Side.SELL,
            intent=TradeIntent.CLOSE,
            quantity=Decimal("0.01"),
            reduce_only=True,
            max_reprices_per_minute=20,
        )
        self.assertEqual(follow.status.value, "draft")

    def test_audit_event_redacts_nested_sensitive_fields(self) -> None:
        event = AuditEvent(
            id=new_id(),
            event_type="order.submitted",
            success=True,
            actor_user_id=self.user.id,
            venue=Venue.BINANCE,
            instrument_key=self.instrument.canonical_key,
            metadata={"context": {"api_secret": "never-log-this"}, "order_count": 1},
        )
        record = event.to_log_record()
        self.assertEqual(record["metadata"]["context"]["api_secret"], "[REDACTED]")
        self.assertEqual(record["metadata"]["order_count"], 1)


class PersistenceFoundationTests(unittest.TestCase):
    def test_schema_inventory_is_complete_without_database_dependency(self) -> None:
        self.assertEqual(
            set(schema_table_names()),
            {
                "users",
                "exchange_accounts",
                "instruments",
                "order_intents",
                "orders",
                "follow_strategies",
                "idempotency_keys",
                "audit_events",
            },
        )

    def test_optional_sqlalchemy_contract(self) -> None:
        if not SQLALCHEMY_AVAILABLE:
            with self.assertRaises(PersistenceDependencyError):
                create_database_engine("sqlite+pysqlite:///:memory:")
            return

        engine = create_database_engine("sqlite+pysqlite:///:memory:")
        try:
            initialize_schema(engine)
            from sqlalchemy import inspect

            self.assertTrue(set(schema_table_names()).issubset(set(inspect(engine).get_table_names())))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
