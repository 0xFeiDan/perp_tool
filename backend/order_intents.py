"""One-time, short-lived order intents.

The browser can display a server-calculated order preview, but it cannot alter
the instrument, side, quantity or reference price that will be executed.  An
intent is deliberately kept server-side and consumed exactly once.
"""
from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


class OrderIntentError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderIntent:
    token: str
    owner: str
    expires_at: float
    payload: dict[str, Any]
    created_at: float

    def public_preview(self) -> dict[str, Any]:
        """Return only display data; the trusted payload remains server-side."""
        return {
            **self.payload["preview"],
            "order_intent_token": self.token,
            "expires_at": int(self.expires_at * 1000),
        }


class OrderIntentStore:
    """In-memory intent store; database persistence is added by the repository layer.

    A process restart invalidates unused intents, which is safe because no order
    has been sent before an intent is consumed.
    """

    def __init__(self, ttl_seconds: int = 30):
        self.ttl_seconds = max(5, min(ttl_seconds, 120))
        self._intents: dict[str, OrderIntent] = {}

    def _purge(self) -> None:
        now = time.time()
        self._intents = {token: item for token, item in self._intents.items() if item.expires_at > now}

    def issue(self, *, owner: str, payload: dict[str, Any]) -> OrderIntent:
        self._purge()
        now = time.time()
        token = secrets.token_urlsafe(32)
        intent = OrderIntent(token=token, owner=owner, created_at=now, expires_at=now + self.ttl_seconds, payload=payload)
        self._intents[token] = intent
        return intent

    def consume(self, *, token: str, owner: str) -> OrderIntent:
        self._purge()
        intent = self._intents.get(token)
        if intent is None:
            raise OrderIntentError("order intent is invalid, expired, or already used")
        if not hmac.compare_digest(intent.owner, owner):
            raise OrderIntentError("order intent belongs to a different authenticated session")
        if intent.expires_at <= time.time():
            raise OrderIntentError("order intent has expired")
        # Consume only after ownership and expiry checks so another authenticated
        # user cannot invalidate a leaked token by guessing its value.
        self._intents.pop(token, None)
        return intent

    def discard(self, *, token: str) -> None:
        """Remove an intent that could not be durably prepared.

        This is intentionally token-only because the token was never returned
        to a browser on the failure path.  It prevents a short-lived orphaned
        in-memory intent when the optional PostgreSQL write rejects it.
        """
        self._intents.pop(token, None)


def decimal_text(value: Decimal) -> str:
    """Canonical decimal representation for audits and intent binding."""
    return format(value, "f")
