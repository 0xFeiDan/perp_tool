"""Vendor-neutral domain records for the trading control plane.

These classes are deliberately independent of FastAPI and SQLAlchemy.  They
are safe to use for request validation, database adapters, background workers,
and tests.  No model has a field for an API secret, private key, session cookie
or raw confirmation token.  Credentials are represented only by a reference
such as ``env:BINANCE_FUTURES_PRIMARY`` or ``vault:prod/binance-primary``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping


UTC = timezone.utc
MAX_ORDER_INTENT_SECONDS = 30


class DomainValidationError(ValueError):
    """Raised before an invalid domain record reaches an exchange adapter."""


class Venue(str, Enum):
    LIGHTER = "lighter"
    HYPERLIQUID = "hyperliquid"
    BINANCE = "binance"
    MT5 = "mt5"


class AccountMode(str, Enum):
    LIVE = "live"
    PAPER = "paper"
    READ_ONLY = "read_only"


class InstrumentType(str, Enum):
    PERPETUAL = "perpetual"
    SPOT = "spot"
    CFD = "cfd"
    FOREX = "forex"
    EQUITY = "equity"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TradeIntent(str, Enum):
    OPEN = "open"
    CLOSE = "close"


class ExecutionMode(str, Enum):
    """``MARKET`` means a BBO-priced IOC, never an unbounded market sweep."""

    MARKET = "market"
    MAKER = "maker"


class OrderStatus(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class IntentStatus(str, Enum):
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    SUBMITTED = "submitted"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REJECTED = "rejected"


class FollowStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    STOPPED = "stopped"
    FAILED = "failed"


_USER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9._:/-]{0,95}$")
_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "private_key",
        "passphrase",
        "password",
        "token",
        "authorization",
        "cookie",
        "signature",
        "credential",
        "mnemonic",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def _non_empty(value: str, field_name: str, max_length: int = 255) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length or any(ord(char) < 32 for char in cleaned):
        raise DomainValidationError(f"invalid {field_name}")
    return cleaned


def _uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as error:
        raise DomainValidationError(f"{field_name} must be a UUID") from error


def _decimal(value: Decimal | int | float | str, field_name: str, *, positive: bool = True) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise DomainValidationError(f"{field_name} must be a decimal") from error
    if not decimal.is_finite() or (decimal <= 0 if positive else decimal < 0):
        qualifier = "positive" if positive else "non-negative"
        raise DomainValidationError(f"{field_name} must be {qualifier}")
    return decimal


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        raise DomainValidationError(f"{field_name} must be a SHA-256 hex digest")
    return value


def confirmation_token_hash(token: str) -> str:
    """Hash a one-time confirmation token; never persist the raw token."""
    if not isinstance(token, str) or len(token) < 16 or len(token) > 512:
        raise DomainValidationError("confirmation token must contain 16 to 512 characters")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_confirmation_token() -> str:
    """Create a browser-safe one-time token.  Persist only its hash."""
    return secrets.token_urlsafe(32)


def idempotency_key_hash(scope: str, key: str) -> str:
    scope = _non_empty(scope, "idempotency scope", 128)
    if not isinstance(key, str) or len(key) < 16 or len(key) > 512:
        raise DomainValidationError("idempotency key must contain 16 to 512 characters")
    return hashlib.sha256(f"{scope}:{key}".encode("utf-8")).hexdigest()


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_sensitive(value: Any) -> Any:
    """Return a JSON-safe copy with credential-like fields removed from logs."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _aware(value, "timestamp").isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _safe_metadata(value: Mapping[str, Any] | None, field_name: str = "metadata") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainValidationError(f"{field_name} must be a mapping")
    for key in value:
        if _is_sensitive_key(key):
            raise DomainValidationError(f"{field_name} must not contain credentials or secrets")
    safe = redact_sensitive(dict(value))
    try:
        encoded = json.dumps(safe, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as error:
        raise DomainValidationError(f"{field_name} must be JSON serializable") from error
    if len(encoded.encode("utf-8")) > 32_768:
        raise DomainValidationError(f"{field_name} is too large")
    return safe


def _credential_reference(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise DomainValidationError("a credential reference is required for a trading account")
        return None
    reference = _non_empty(value, "credential reference", 255)
    if not reference.startswith(("env:", "vault:", "secret://", "keyring:")):
        raise DomainValidationError("credential reference must point to env, vault, secret manager, or keyring")
    return reference


@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str
    email: str
    role: str = "operator"
    is_active: bool = True
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "user id"))
        username = _non_empty(self.username, "username", 64)
        if not _USER_NAME.fullmatch(username):
            raise DomainValidationError("username contains unsupported characters")
        object.__setattr__(self, "username", username)
        email = _non_empty(self.email, "email", 254).lower()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise DomainValidationError("email must look like an email address")
        object.__setattr__(self, "email", email)
        role = _non_empty(self.role, "role", 32).lower()
        if role not in {"admin", "operator", "viewer", "auditor"}:
            raise DomainValidationError("unsupported user role")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class ExchangeAccount:
    id: str
    user_id: str
    venue: Venue
    label: str
    mode: AccountMode
    credential_reference: str | None = None
    is_active: bool = True
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "exchange account id"))
        object.__setattr__(self, "user_id", _uuid(self.user_id, "user id"))
        object.__setattr__(self, "venue", Venue(self.venue))
        object.__setattr__(self, "mode", AccountMode(self.mode))
        object.__setattr__(self, "label", _non_empty(self.label, "account label", 80))
        if self.venue is Venue.MT5 and self.mode is not AccountMode.READ_ONLY:
            raise DomainValidationError("MT5 accounts are read-only in this control plane")
        required = self.mode is AccountMode.LIVE and self.venue is not Venue.MT5
        object.__setattr__(self, "credential_reference", _credential_reference(self.credential_reference, required=required))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))

    @property
    def can_trade(self) -> bool:
        return self.is_active and self.mode is AccountMode.LIVE and self.venue is not Venue.MT5


@dataclass(frozen=True, slots=True)
class Instrument:
    id: str
    venue: Venue
    external_id: str
    symbol: str
    instrument_type: InstrumentType = InstrumentType.PERPETUAL
    base_asset: str | None = None
    quote_asset: str | None = None
    min_notional: Decimal | None = None
    quantity_step: Decimal | None = None
    price_tick: Decimal | None = None
    is_active: bool = True
    metadata: Mapping[str, Any] | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "instrument id"))
        object.__setattr__(self, "venue", Venue(self.venue))
        object.__setattr__(self, "instrument_type", InstrumentType(self.instrument_type))
        object.__setattr__(self, "external_id", _non_empty(self.external_id, "external instrument id", 128))
        symbol = _non_empty(self.symbol, "symbol", 96).upper()
        if not _SYMBOL.fullmatch(symbol):
            raise DomainValidationError("symbol contains unsupported characters")
        object.__setattr__(self, "symbol", symbol)
        for field_name in ("base_asset", "quote_asset"):
            value = getattr(self, field_name)
            if value is not None:
                asset = _non_empty(value, field_name, 24).upper()
                if not re.fullmatch(r"[A-Z0-9._-]{1,24}", asset):
                    raise DomainValidationError(f"invalid {field_name}")
                object.__setattr__(self, field_name, asset)
        for field_name in ("min_notional", "quantity_step", "price_tick"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _decimal(value, field_name))
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))

    @property
    def canonical_key(self) -> str:
        """Stable key used to prevent cross-venue symbol confusion."""
        return f"{self.venue.value}:{self.external_id}".lower()


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A short-lived, one-time, confirmed description of an intended order."""

    id: str
    user_id: str
    exchange_account_id: str
    instrument_id: str
    venue: Venue
    instrument_key: str
    side: Side
    intent: TradeIntent
    mode: ExecutionMode
    notional: Decimal
    confirmation_token_hash: str
    request_fingerprint: str
    expires_at: datetime
    status: IntentStatus = IntentStatus.PENDING_CONFIRMATION
    created_at: datetime = field(default_factory=utc_now)
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("id", "user_id", "exchange_account_id", "instrument_id"):
            object.__setattr__(self, field_name, _uuid(getattr(self, field_name), field_name))
        object.__setattr__(self, "venue", Venue(self.venue))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "intent", TradeIntent(self.intent))
        object.__setattr__(self, "mode", ExecutionMode(self.mode))
        expected_key = _non_empty(self.instrument_key, "instrument key", 180).lower()
        if not expected_key.startswith(f"{self.venue.value}:"):
            raise DomainValidationError("instrument key does not belong to the selected venue")
        object.__setattr__(self, "instrument_key", expected_key)
        object.__setattr__(self, "notional", _decimal(self.notional, "notional"))
        object.__setattr__(self, "confirmation_token_hash", _sha256(self.confirmation_token_hash, "confirmation token hash"))
        object.__setattr__(self, "request_fingerprint", _sha256(self.request_fingerprint, "request fingerprint"))
        created_at = _aware(self.created_at, "created_at")
        expires_at = _aware(self.expires_at, "expires_at")
        if expires_at <= created_at or expires_at - created_at > timedelta(seconds=MAX_ORDER_INTENT_SECONDS):
            raise DomainValidationError(f"order intent must expire within {MAX_ORDER_INTENT_SECONDS} seconds")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "status", IntentStatus(self.status))
        if self.confirmed_at is not None:
            confirmed_at = _aware(self.confirmed_at, "confirmed_at")
            if confirmed_at > expires_at:
                raise DomainValidationError("confirmed_at may not be after expiry")
            object.__setattr__(self, "confirmed_at", confirmed_at)

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        exchange_account_id: str,
        instrument_id: str,
        venue: Venue,
        instrument_key: str,
        side: Side,
        intent: TradeIntent,
        mode: ExecutionMode,
        notional: Decimal,
        confirmation_token: str,
        request_fingerprint: str,
        now: datetime | None = None,
        expires_in_seconds: int = MAX_ORDER_INTENT_SECONDS,
    ) -> "OrderIntent":
        if not 1 <= expires_in_seconds <= MAX_ORDER_INTENT_SECONDS:
            raise DomainValidationError(f"expires_in_seconds must be between 1 and {MAX_ORDER_INTENT_SECONDS}")
        created_at = _aware(now or utc_now(), "now")
        return cls(
            id=new_id(),
            user_id=user_id,
            exchange_account_id=exchange_account_id,
            instrument_id=instrument_id,
            venue=venue,
            instrument_key=instrument_key,
            side=side,
            intent=intent,
            mode=mode,
            notional=notional,
            confirmation_token_hash=confirmation_token_hash(confirmation_token),
            request_fingerprint=request_fingerprint,
            expires_at=created_at + timedelta(seconds=expires_in_seconds),
            created_at=created_at,
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        return _aware(now or utc_now(), "now") >= self.expires_at

    def confirm(self, token: str, now: datetime | None = None) -> "OrderIntent":
        now = _aware(now or utc_now(), "now")
        if self.status is not IntentStatus.PENDING_CONFIRMATION:
            raise DomainValidationError("order intent is not waiting for confirmation")
        if self.is_expired(now):
            raise DomainValidationError("order intent expired")
        if not hmac.compare_digest(confirmation_token_hash(token), self.confirmation_token_hash):
            raise DomainValidationError("order intent confirmation token is invalid")
        return replace(self, status=IntentStatus.CONFIRMED, confirmed_at=now)

    def consume(self, now: datetime | None = None) -> "OrderIntent":
        now = _aware(now or utc_now(), "now")
        if self.status is not IntentStatus.CONFIRMED or self.is_expired(now):
            raise DomainValidationError("order intent cannot be consumed")
        return replace(self, status=IntentStatus.CONSUMED)


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    order_intent_id: str
    exchange_account_id: str
    instrument_id: str
    venue: Venue
    instrument_key: str
    client_order_id: str
    side: Side
    intent: TradeIntent
    mode: ExecutionMode
    quantity: Decimal
    limit_price: Decimal
    reduce_only: bool
    status: OrderStatus = OrderStatus.CREATED
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for field_name in ("id", "order_intent_id", "exchange_account_id", "instrument_id"):
            object.__setattr__(self, field_name, _uuid(getattr(self, field_name), field_name))
        object.__setattr__(self, "venue", Venue(self.venue))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "intent", TradeIntent(self.intent))
        object.__setattr__(self, "mode", ExecutionMode(self.mode))
        key = _non_empty(self.instrument_key, "instrument key", 180).lower()
        if not key.startswith(f"{self.venue.value}:"):
            raise DomainValidationError("instrument key does not belong to the selected venue")
        object.__setattr__(self, "instrument_key", key)
        client_order_id = _non_empty(self.client_order_id, "client order id", 64)
        if not _CLIENT_ORDER_ID.fullmatch(client_order_id):
            raise DomainValidationError("client order id contains unsupported characters")
        object.__setattr__(self, "client_order_id", client_order_id)
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))
        object.__setattr__(self, "limit_price", _decimal(self.limit_price, "limit_price"))
        object.__setattr__(self, "filled_quantity", _decimal(self.filled_quantity, "filled_quantity", positive=False))
        if self.filled_quantity > self.quantity:
            raise DomainValidationError("filled quantity cannot exceed order quantity")
        if self.intent is TradeIntent.CLOSE and not self.reduce_only:
            raise DomainValidationError("close orders must be reduce-only")
        if self.intent is TradeIntent.OPEN and self.reduce_only:
            raise DomainValidationError("open orders cannot be reduce-only")
        object.__setattr__(self, "status", OrderStatus(self.status))
        if self.exchange_order_id is not None:
            object.__setattr__(self, "exchange_order_id", _non_empty(self.exchange_order_id, "exchange order id", 128))
        created_at = _aware(self.created_at, "created_at")
        updated_at = _aware(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise DomainValidationError("updated_at may not be before created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.limit_price

    def with_status(
        self,
        status: OrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        exchange_order_id: str | None = None,
        now: datetime | None = None,
    ) -> "Order":
        return replace(
            self,
            status=status,
            filled_quantity=self.filled_quantity if filled_quantity is None else filled_quantity,
            exchange_order_id=self.exchange_order_id if exchange_order_id is None else exchange_order_id,
            updated_at=_aware(now or utc_now(), "now"),
        )


@dataclass(frozen=True, slots=True)
class FollowStrategy:
    """One exact, post-only BBO follower.  It never represents an unknown order."""

    id: str
    user_id: str
    exchange_account_id: str
    instrument_id: str
    venue: Venue
    instrument_key: str
    side: Side
    intent: TradeIntent
    quantity: Decimal
    reduce_only: bool
    max_reprices_per_minute: int
    status: FollowStatus = FollowStatus.DRAFT
    active_order_id: str | None = None
    last_price: Decimal | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for field_name in ("id", "user_id", "exchange_account_id", "instrument_id"):
            object.__setattr__(self, field_name, _uuid(getattr(self, field_name), field_name))
        object.__setattr__(self, "venue", Venue(self.venue))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "intent", TradeIntent(self.intent))
        key = _non_empty(self.instrument_key, "instrument key", 180).lower()
        if not key.startswith(f"{self.venue.value}:"):
            raise DomainValidationError("instrument key does not belong to the selected venue")
        object.__setattr__(self, "instrument_key", key)
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))
        if self.intent is TradeIntent.CLOSE and not self.reduce_only:
            raise DomainValidationError("close follow strategies must be reduce-only")
        if self.intent is TradeIntent.OPEN and self.reduce_only:
            raise DomainValidationError("open follow strategies cannot be reduce-only")
        if not 1 <= int(self.max_reprices_per_minute) <= 120:
            raise DomainValidationError("max_reprices_per_minute must be between 1 and 120")
        object.__setattr__(self, "max_reprices_per_minute", int(self.max_reprices_per_minute))
        object.__setattr__(self, "status", FollowStatus(self.status))
        if self.active_order_id is not None:
            object.__setattr__(self, "active_order_id", _uuid(self.active_order_id, "active order id"))
        if self.last_price is not None:
            object.__setattr__(self, "last_price", _decimal(self.last_price, "last_price"))
        created_at = _aware(self.created_at, "created_at")
        updated_at = _aware(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise DomainValidationError("updated_at may not be before created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    def activate(self, order_id: str, price: Decimal, now: datetime | None = None) -> "FollowStrategy":
        if self.status not in {FollowStatus.DRAFT, FollowStatus.PAUSED}:
            raise DomainValidationError("only a draft or paused follow strategy can be activated")
        return replace(
            self,
            status=FollowStatus.ACTIVE,
            active_order_id=_uuid(order_id, "active order id"),
            last_price=_decimal(price, "last_price"),
            updated_at=_aware(now or utc_now(), "now"),
        )


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    id: str
    scope: str
    key_hash: str
    request_fingerprint: str
    expires_at: datetime
    status: str = "claimed"
    response_reference: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "idempotency id"))
        object.__setattr__(self, "scope", _non_empty(self.scope, "idempotency scope", 128))
        object.__setattr__(self, "key_hash", _sha256(self.key_hash, "idempotency key hash"))
        object.__setattr__(self, "request_fingerprint", _sha256(self.request_fingerprint, "request fingerprint"))
        created_at = _aware(self.created_at, "created_at")
        expires_at = _aware(self.expires_at, "expires_at")
        if expires_at <= created_at:
            raise DomainValidationError("idempotency key must expire after creation")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "status", _non_empty(self.status, "idempotency status", 32).lower())
        if self.response_reference is not None:
            object.__setattr__(self, "response_reference", _non_empty(self.response_reference, "response reference", 128))

    @classmethod
    def claim(
        cls,
        *,
        scope: str,
        key: str,
        request_fingerprint: str,
        now: datetime | None = None,
        expires_in_seconds: int = 15 * 60,
    ) -> "IdempotencyKey":
        if not 1 <= expires_in_seconds <= 24 * 60 * 60:
            raise DomainValidationError("idempotency expiry must be between 1 second and 24 hours")
        created_at = _aware(now or utc_now(), "now")
        return cls(
            id=new_id(),
            scope=scope,
            key_hash=idempotency_key_hash(scope, key),
            request_fingerprint=request_fingerprint,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=expires_in_seconds),
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    event_type: str
    success: bool
    actor_user_id: str | None = None
    venue: Venue | None = None
    instrument_key: str | None = None
    order_id: str | None = None
    metadata: Mapping[str, Any] | None = None
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "audit event id"))
        event_type = _non_empty(self.event_type, "event type", 96).lower()
        if not _EVENT_TYPE.fullmatch(event_type):
            raise DomainValidationError("event type contains unsupported characters")
        object.__setattr__(self, "event_type", event_type)
        if self.actor_user_id is not None:
            object.__setattr__(self, "actor_user_id", _uuid(self.actor_user_id, "actor user id"))
        if self.venue is not None:
            object.__setattr__(self, "venue", Venue(self.venue))
        if self.instrument_key is not None:
            key = _non_empty(self.instrument_key, "instrument key", 180).lower()
            if self.venue is not None and not key.startswith(f"{self.venue.value}:"):
                raise DomainValidationError("audit instrument key does not belong to venue")
            object.__setattr__(self, "instrument_key", key)
        if self.order_id is not None:
            object.__setattr__(self, "order_id", _uuid(self.order_id, "order id"))
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata, "audit metadata"))
        object.__setattr__(self, "occurred_at", _aware(self.occurred_at, "occurred_at"))

    def to_log_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "success": self.success,
            "actor_user_id": self.actor_user_id,
            "venue": self.venue.value if self.venue else None,
            "instrument_key": self.instrument_key,
            "order_id": self.order_id,
            "metadata": redact_sensitive(self.metadata),
            "occurred_at": self.occurred_at.isoformat(),
        }


__all__ = [
    "MAX_ORDER_INTENT_SECONDS",
    "AccountMode",
    "AuditEvent",
    "DomainValidationError",
    "ExchangeAccount",
    "ExecutionMode",
    "FollowStatus",
    "FollowStrategy",
    "IdempotencyKey",
    "Instrument",
    "InstrumentType",
    "IntentStatus",
    "Order",
    "OrderIntent",
    "OrderStatus",
    "Side",
    "TradeIntent",
    "User",
    "Venue",
    "confirmation_token_hash",
    "create_confirmation_token",
    "idempotency_key_hash",
    "new_id",
    "redact_sensitive",
    "utc_now",
]
