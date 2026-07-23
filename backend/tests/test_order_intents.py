from __future__ import annotations

import time

import pytest

from order_intents import OrderIntentError, OrderIntentStore


def test_intent_is_single_use_and_owner_bound() -> None:
    store = OrderIntentStore(ttl_seconds=30)
    intent = store.issue(owner="operator-a", payload={"preview": {"market_id": "120"}})

    with pytest.raises(OrderIntentError, match="different authenticated"):
        store.consume(token=intent.token, owner="operator-b")

    consumed = store.consume(token=intent.token, owner="operator-a")
    assert consumed.payload["preview"]["market_id"] == "120"
    with pytest.raises(OrderIntentError, match="invalid"):
        store.consume(token=intent.token, owner="operator-a")


def test_expired_intent_never_executes() -> None:
    store = OrderIntentStore(ttl_seconds=5)
    intent = store.issue(owner="operator-a", payload={"preview": {}})
    # Test the expiration invariant without sleeping in the test suite.
    store._intents[intent.token] = type(intent)(
        token=intent.token,
        owner=intent.owner,
        created_at=intent.created_at,
        expires_at=time.time() - 1,
        payload=intent.payload,
    )
    with pytest.raises(OrderIntentError, match="invalid"):
        store.consume(token=intent.token, owner="operator-a")
