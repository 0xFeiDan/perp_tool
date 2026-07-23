"""Offline tests for the optional in-memory-to-PostgreSQL bridge."""
from __future__ import annotations

import sys
import unittest
import uuid
from decimal import Decimal
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from domain_models import OrderStatus  # noqa: E402
from persistence import SQLALCHEMY_AVAILABLE  # noqa: E402
from repository import OrderIntentNotConsumable  # noqa: E402
from runtime_persistence_bridge import (  # noqa: E402
    RuntimePersistenceBridge,
    RuntimePersistenceBridgeError,
    bridge_if_configured,
)


def runtime_instrument(*, venue: str = "binance") -> dict[str, object]:
    return {
        "internal_instrument_id": str(uuid.uuid5(uuid.UUID("d33e9c8d-ae4d-4a16-af1c-3c271a05a2aa"), f"{venue}:BTCUSDT")),
        "venue": venue,
        "external_id": "BTCUSDT",
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "settle_asset": "USDT",
        "min_notional": "5",
        "quantity_step": "0.001",
        "price_tick": "0.1",
        "status": "active",
        "metadata": {"untrusted_secret": "must-not-be-carried"},
    }


class DisabledBridgeTests(unittest.TestCase):
    def test_missing_database_keeps_runtime_flow_enabled_without_persistence(self) -> None:
        bridge = bridge_if_configured(None)
        self.assertFalse(bridge.enabled)
        context = bridge.prepare_intent(
            venue="binance",
            internal_instrument=runtime_instrument(),
            side="buy",
            intent="open",
            mode="market",
            notional="100",
            raw_token="token-that-is-only-kept-by-the-inmemory-store-123",
        )
        self.assertFalse(context.persistence_enabled)
        self.assertIsNone(context.persistent_intent_id)
        claim = bridge.claim_execution(context, request_id="disabled-bridge-request-id-123456")
        self.assertTrue(claim.claimed)
        submission = bridge.consume_for_submission(context, raw_token="token-that-is-only-kept-by-the-inmemory-store-123")
        order = bridge.pre_submit_order(
            submission,
            client_order_id="disabled-order-1",
            quantity="0.001",
            limit_price="100000",
        )
        self.assertFalse(order.persistence_enabled)
        submitted = bridge.mark_order_submitted(order, exchange_order_id="exchange-order-1")
        self.assertEqual(submitted.status, "submitted")

    def test_cross_venue_runtime_instrument_is_rejected(self) -> None:
        bridge = RuntimePersistenceBridge()
        with self.assertRaises(RuntimePersistenceBridgeError):
            bridge.prepare_intent(
                venue="lighter",
                internal_instrument=runtime_instrument(venue="binance"),
                side="buy",
                intent="open",
                mode="market",
                notional="100",
                raw_token="token-that-is-only-kept-by-the-inmemory-store-123",
            )


@unittest.skipUnless(SQLALCHEMY_AVAILABLE, "SQLAlchemy optional dependency is not installed")
class PersistentBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = bridge_if_configured("sqlite+pysqlite:///:memory:", initialize_schema=True)
        self.token = "token-that-is-only-kept-by-the-inmemory-store-123"
        self.context = self.bridge.prepare_intent(
            venue="binance",
            internal_instrument=runtime_instrument(),
            side="buy",
            intent="open",
            mode="market",
            notional="100",
            raw_token=self.token,
        )

    def tearDown(self) -> None:
        self.bridge.dispose()

    def test_durable_intent_idempotency_and_order_lifecycle(self) -> None:
        self.assertTrue(self.context.persistence_enabled)
        self.assertIsNotNone(self.context.persistent_intent_id)
        first = self.bridge.claim_execution(self.context, request_id="persistent-bridge-request-id-123456")
        duplicate = self.bridge.claim_execution(self.context, request_id="persistent-bridge-request-id-123456")
        self.assertTrue(first.claimed)
        self.assertFalse(duplicate.claimed)

        submission = self.bridge.consume_for_submission(self.context, raw_token=self.token)
        self.assertTrue(submission.persistent_consumed)
        with self.assertRaises(OrderIntentNotConsumable):
            self.bridge.consume_for_submission(self.context, raw_token=self.token)

        order = self.bridge.pre_submit_order(
            submission,
            client_order_id="persistent-order-1",
            quantity=Decimal("0.001"),
            limit_price=Decimal("100000"),
        )
        self.assertEqual(order.status, "created")
        submitted = self.bridge.mark_order_submitted(order, exchange_order_id="exchange-order-1")
        filled = self.bridge.record_order_status(
            submitted,
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("0.001"),
            audit_metadata={"adapter": "offline-test", "nested": {"api_secret": "redacted-by-domain"}},
        )
        self.assertEqual(filled.status, "filled")
        self.assertEqual(filled.exchange_order_id, "exchange-order-1")
        complete = self.bridge.complete_execution(first, status="completed", response_reference=filled.persistent_order_id)
        self.assertEqual(complete.status, "completed")

        from sqlalchemy import select
        from persistence import AuditEventRecord, OrderIntentRecord

        with self.bridge._repository._sessions() as session:  # test-only read of durable redaction guarantees
            stored_intent = session.execute(
                select(OrderIntentRecord).where(OrderIntentRecord.id == self.context.persistent_intent_id)
            ).scalar_one()
            latest_audit = session.execute(
                select(AuditEventRecord)
                .where(AuditEventRecord.order_id == filled.persistent_order_id)
                .order_by(AuditEventRecord.occurred_at.desc())
            ).scalars().first()
        self.assertNotEqual(stored_intent.confirmation_token_hash, self.token)
        self.assertEqual(len(stored_intent.confirmation_token_hash), 64)
        self.assertEqual(latest_audit.metadata_json["detail"]["nested"]["api_secret"], "[REDACTED]")

    def test_same_operator_account_and_instrument_mapping_is_deterministic(self) -> None:
        second = self.bridge.prepare_intent(
            venue="binance",
            internal_instrument=runtime_instrument(),
            side="sell",
            intent="close",
            mode="maker",
            notional="100",
            raw_token="second-token-that-is-only-kept-in-memory-456",
        )
        self.assertEqual(second.user_id, self.context.user_id)
        self.assertEqual(second.exchange_account_id, self.context.exchange_account_id)
        self.assertEqual(second.instrument_id, self.context.instrument_id)
        self.assertNotEqual(second.persistent_intent_id, self.context.persistent_intent_id)


if __name__ == "__main__":
    unittest.main()
