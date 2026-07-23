"""Offline repository tests; no .env, network, or exchange requests are used."""
from __future__ import annotations

import hashlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from domain_models import (  # noqa: E402
    AccountMode,
    DomainValidationError,
    ExchangeAccount,
    ExecutionMode,
    Instrument,
    Order,
    OrderIntent,
    OrderStatus,
    Side,
    TradeIntent,
    User,
    Venue,
    create_confirmation_token,
    new_id,
)
from persistence import PersistenceDependencyError, SQLALCHEMY_AVAILABLE  # noqa: E402
from repository import (  # noqa: E402
    OrderIntentNotConsumable,
    PersistenceRepository,
    repository_if_configured,
)


def fingerprint() -> str:
    return hashlib.sha256(b"offline-repository-test").hexdigest()


class RepositoryConfigurationTests(unittest.TestCase):
    def test_missing_database_url_disables_repository_without_connecting(self) -> None:
        self.assertIsNone(repository_if_configured(None))
        self.assertIsNone(repository_if_configured("   "))

    def test_uninstalled_sqlalchemy_fails_only_when_explicitly_enabled(self) -> None:
        if SQLALCHEMY_AVAILABLE:
            self.skipTest("SQLAlchemy is installed; database path is covered below")
        with self.assertRaises(PersistenceDependencyError):
            repository_if_configured("postgresql+psycopg://not-used.example/control")


@unittest.skipUnless(SQLALCHEMY_AVAILABLE, "SQLAlchemy optional dependency is not installed")
class PersistentRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PersistenceRepository.from_database_url("sqlite+pysqlite:///:memory:", initialize_schema=True)
        self.user = User(id=new_id(), username="sole_operator", email="operator@example.test")
        self.account = ExchangeAccount(
            id=new_id(),
            user_id=self.user.id,
            venue=Venue.BINANCE,
            label="futures-primary",
            mode=AccountMode.LIVE,
            credential_reference="env:BINANCE_FUTURES_PRIMARY",
        )
        self.instrument = Instrument(
            id=new_id(),
            venue=Venue.BINANCE,
            external_id="BTCUSDT",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            min_notional=Decimal("5"),
        )
        self.repository.bootstrap_single_operator(
            user=self.user,
            account=self.account,
            instruments=[self.instrument],
        )

    def tearDown(self) -> None:
        self.repository.dispose()

    def _new_intent(self, token: str) -> OrderIntent:
        return OrderIntent.create(
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
            request_fingerprint=fingerprint(),
        )

    def test_intent_consumes_once_then_order_status_is_audited(self) -> None:
        token = create_confirmation_token()
        intent = self._new_intent(token)
        self.repository.store_order_intent(intent)
        self.repository.confirm_order_intent(intent.id, token)
        consumed = self.repository.consume_confirmed_order_intent(intent.id)
        self.assertEqual(consumed.instrument_key, self.instrument.canonical_key)
        with self.assertRaises(OrderIntentNotConsumable):
            self.repository.consume_confirmed_order_intent(intent.id)

        order = Order(
            id=new_id(),
            order_intent_id=intent.id,
            exchange_account_id=self.account.id,
            instrument_id=self.instrument.id,
            venue=Venue.BINANCE,
            instrument_key=self.instrument.canonical_key,
            client_order_id="offline-order-1",
            side=Side.BUY,
            intent=TradeIntent.OPEN,
            mode=ExecutionMode.MARKET,
            quantity=Decimal("0.001"),
            limit_price=Decimal("100000"),
            reduce_only=False,
        )
        self.repository.store_order(order)
        submitted = self.repository.record_order_status(order.id, OrderStatus.SUBMITTED)
        filled = self.repository.record_order_status(
            order.id,
            OrderStatus.FILLED,
            exchange_order_id="exchange-order-1",
            filled_quantity=Decimal("0.001"),
        )
        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(filled.status, "filled")

        from sqlalchemy import select
        from persistence import AuditEventRecord

        with self.repository._sessions() as session:  # test-only read of durable audit rows
            rows = session.execute(select(AuditEventRecord).where(AuditEventRecord.order_id == order.id)).scalars().all()
        self.assertEqual([row.event_type for row in rows], ["order.created", "order.status_changed", "order.status_changed"])

    def test_idempotency_is_persistent_and_raw_key_is_never_stored(self) -> None:
        raw_key = "order-request-idempotency-123456"
        first = self.repository.claim_idempotency(
            scope=f"execute:{self.account.id}",
            key=raw_key,
            request_fingerprint=fingerprint(),
        )
        second = self.repository.claim_idempotency(
            scope=f"execute:{self.account.id}",
            key=raw_key,
            request_fingerprint=fingerprint(),
        )
        self.assertTrue(first.claimed)
        self.assertFalse(second.claimed)
        done = self.repository.complete_idempotency(first.id, response_reference="order:offline-order-1")
        self.assertEqual(done.status, "completed")

        from sqlalchemy import select
        from persistence import IdempotencyKeyRecord

        with self.repository._sessions() as session:  # test-only read of hashed durable data
            stored = session.execute(select(IdempotencyKeyRecord).where(IdempotencyKeyRecord.id == first.id)).scalar_one()
        self.assertNotEqual(stored.key_hash, raw_key)
        self.assertEqual(len(stored.key_hash), 64)

    def test_bootstrap_refuses_cross_venue_instrument_mapping(self) -> None:
        invalid = Instrument(id=new_id(), venue=Venue.LIGHTER, external_id="120", symbol="LIT-USD")
        with self.assertRaises(DomainValidationError):
            self.repository.bootstrap_single_operator(user=self.user, account=self.account, instruments=[invalid])


if __name__ == "__main__":
    unittest.main()
