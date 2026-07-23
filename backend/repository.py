"""Optional PostgreSQL-backed repository for durable trading-control state.

This module is intentionally not imported by ``main.py``.  A caller can keep
the current in-memory mode by passing no DATABASE_URL, or explicitly create a
repository with ``repository_if_configured(os.getenv("DATABASE_URL"))``.

Raw API credentials, private keys, browser cookies, and one-time confirmation
tokens are never persisted here.  Confirmation/idempotency values are hashed
before the database transaction begins.
"""
from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

try:  # Supports both direct backend execution and package imports.
    from . import persistence as _persistence
    from .domain_models import (
        AuditEvent,
        DomainValidationError,
        ExchangeAccount,
        FollowStrategy,
        IdempotencyKey,
        Instrument,
        IntentStatus,
        Order,
        OrderIntent,
        OrderStatus,
        User,
        confirmation_token_hash,
        utc_now,
    )
except ImportError:  # pragma: no cover - used by the current direct app-dir launch style
    import persistence as _persistence
    from domain_models import (
        AuditEvent,
        DomainValidationError,
        ExchangeAccount,
        FollowStrategy,
        IdempotencyKey,
        Instrument,
        IntentStatus,
        Order,
        OrderIntent,
        OrderStatus,
        User,
        confirmation_token_hash,
        utc_now,
    )


UTC = timezone.utc


class RepositoryError(RuntimeError):
    """Base error for a durable repository operation."""


class RepositoryConflict(RepositoryError):
    """A uniqueness or state-conflict prevented a safe write."""


class RecordNotFound(RepositoryError):
    """The requested durable record does not exist."""


class OrderIntentNotConsumable(RepositoryConflict):
    """A one-time intent has not been confirmed, has expired, or was consumed."""


class OrderStateTransitionError(RepositoryConflict):
    """Order state was not allowed to move to the requested state."""


if _persistence.SQLALCHEMY_AVAILABLE:
    from sqlalchemy import select, text
    from sqlalchemy.exc import IntegrityError

    AuditEventRecord = _persistence.AuditEventRecord
    ExchangeAccountRecord = _persistence.ExchangeAccountRecord
    FollowStrategyRecord = _persistence.FollowStrategyRecord
    IdempotencyKeyRecord = _persistence.IdempotencyKeyRecord
    InstrumentRecord = _persistence.InstrumentRecord
    OrderIntentRecord = _persistence.OrderIntentRecord
    OrderRecord = _persistence.OrderRecord
    PortfolioEquitySnapshotRecord = _persistence.PortfolioEquitySnapshotRecord
    UserRecord = _persistence.UserRecord


@dataclass(frozen=True, slots=True)
class ConsumedOrderIntent:
    """Execution-safe intent details returned after an atomic one-time consume."""

    id: str
    user_id: str
    exchange_account_id: str
    instrument_id: str
    venue: str
    instrument_key: str
    side: str
    intent: str
    mode: str
    notional: Decimal
    request_fingerprint: str
    expires_at: datetime
    consumed_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """The caller must execute only when ``claimed`` is true."""

    id: str
    claimed: bool
    status: str
    response_reference: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OrderStateSnapshot:
    id: str
    order_intent_id: str
    status: str
    exchange_order_id: str | None
    filled_quantity: Decimal
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PortfolioEquitySnapshot:
    """One real, USDC/USDT 1:1 account-equity observation."""

    bucket_start: datetime
    observed_at: datetime
    total_equity: Decimal
    available_margin: Decimal
    unrealized_pnl: Decimal
    synced_venues: int


@dataclass(frozen=True, slots=True)
class BootstrapMapping:
    """Stable IDs a runtime adapter can cache without holding credentials."""

    user_id: str
    exchange_account_id: str
    instrument_ids: Mapping[str, str]


_TERMINAL_ORDER_STATES = {
    OrderStatus.FILLED.value,
    OrderStatus.CANCELED.value,
    OrderStatus.REJECTED.value,
}
_ORDER_TRANSITIONS: dict[str, set[str]] = {
    OrderStatus.CREATED.value: {
        OrderStatus.CREATED.value,
        OrderStatus.SUBMITTED.value,
        OrderStatus.ACKNOWLEDGED.value,
        OrderStatus.CANCEL_PENDING.value,
        OrderStatus.CANCELED.value,
        OrderStatus.REJECTED.value,
        OrderStatus.UNKNOWN.value,
    },
    OrderStatus.SUBMITTED.value: {
        OrderStatus.SUBMITTED.value,
        OrderStatus.ACKNOWLEDGED.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.FILLED.value,
        OrderStatus.CANCEL_PENDING.value,
        OrderStatus.CANCELED.value,
        OrderStatus.REJECTED.value,
        OrderStatus.UNKNOWN.value,
    },
    OrderStatus.ACKNOWLEDGED.value: {
        OrderStatus.ACKNOWLEDGED.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.FILLED.value,
        OrderStatus.CANCEL_PENDING.value,
        OrderStatus.CANCELED.value,
        OrderStatus.REJECTED.value,
        OrderStatus.UNKNOWN.value,
    },
    OrderStatus.PARTIALLY_FILLED.value: {
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.FILLED.value,
        OrderStatus.CANCEL_PENDING.value,
        OrderStatus.CANCELED.value,
        OrderStatus.UNKNOWN.value,
    },
    OrderStatus.CANCEL_PENDING.value: {
        OrderStatus.CANCEL_PENDING.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.FILLED.value,
        OrderStatus.CANCELED.value,
        OrderStatus.UNKNOWN.value,
    },
    OrderStatus.UNKNOWN.value: {
        OrderStatus.UNKNOWN.value,
        OrderStatus.ACKNOWLEDGED.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.FILLED.value,
        OrderStatus.CANCEL_PENDING.value,
        OrderStatus.CANCELED.value,
        OrderStatus.REJECTED.value,
    },
    OrderStatus.FILLED.value: {OrderStatus.FILLED.value},
    OrderStatus.CANCELED.value: {OrderStatus.CANCELED.value},
    OrderStatus.REJECTED.value: {OrderStatus.REJECTED.value},
}


def _as_utc(value: datetime | None, field_name: str) -> datetime:
    if value is None:
        return utc_now()
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    # PostgreSQL returns tz-aware values; treating a SQLite test value as UTC is
    # safe because test engines have no external timezone source.
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: Any, field_name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:  # Decimal may raise implementation-specific errors.
        raise ValueError(f"{field_name} must be a decimal") from error
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise ValueError(f"{field_name} is outside its allowed range")
    return result


def _signed_decimal(value: Any, field_name: str) -> Decimal:
    """Parse a finite account value while allowing negative PnL."""
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"{field_name} must be a decimal") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _public_reference(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 128:
        raise ValueError(f"invalid {field_name}")
    if any(term in cleaned.lower() for term in ("secret", "token", "password", "private")):
        raise ValueError(f"{field_name} must not contain credential material")
    return cleaned


class PersistenceRepository:
    """A transaction boundary around optional SQLAlchemy/PostgreSQL storage.

    The repository does not perform exchange I/O.  Its most important method,
    :meth:`consume_confirmed_order_intent`, uses PostgreSQL ``SELECT ... FOR
    UPDATE`` and changes the intent state inside the same transaction.  Two
    simultaneous requests therefore cannot both obtain an executable intent.
    """

    def __init__(self, engine: Any):
        _persistence.require_sqlalchemy()
        if engine is None:
            raise ValueError("database engine is required")
        self._engine = engine
        self._sessions = _persistence.make_session_factory(engine)

    @classmethod
    def from_database_url(cls, database_url: str, *, initialize_schema: bool = False) -> "PersistenceRepository":
        engine = _persistence.create_database_engine(database_url)
        if initialize_schema:
            _persistence.initialize_schema(engine)
        return cls(engine)

    @property
    def engine(self) -> Any:
        return self._engine

    def dispose(self) -> None:
        self._engine.dispose()

    def ping(self) -> None:
        """Verify that the configured database is reachable without logging its DSN.

        A real execution path calls this during startup when durable execution
        is required.  It deliberately performs no schema changes and returns
        no database values, so a connection problem cannot leak credentials or
        application data through the health path.
        """
        with self._engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def upsert_portfolio_equity_snapshot(
        self,
        *,
        observed_at: datetime,
        total_equity: Decimal | str | int | float,
        available_margin: Decimal | str | int | float,
        unrealized_pnl: Decimal | str | int | float,
        synced_venues: int,
        bucket_minutes: int = 5,
    ) -> PortfolioEquitySnapshot:
        """Store one verified aggregate equity point per time bucket.

        The series contains no exchange credentials, account identifiers or
        position details.  It is intentionally separate from order/audit data
        so an account page refresh cannot be mistaken for trade history.
        """
        observed = _as_utc(observed_at, "observed_at")
        if not isinstance(bucket_minutes, int) or bucket_minutes < 1 or bucket_minutes > 60:
            raise ValueError("bucket_minutes must be between 1 and 60")
        if not isinstance(synced_venues, int) or synced_venues < 1 or synced_venues > 3:
            raise ValueError("synced_venues must be between 1 and 3")
        equity = _decimal(total_equity, "total_equity")
        available = _decimal(available_margin, "available_margin")
        pnl = _signed_decimal(unrealized_pnl, "unrealized_pnl")
        bucket = observed.replace(minute=observed.minute - (observed.minute % bucket_minutes), second=0, microsecond=0)
        with self._sessions() as session:
            with session.begin():
                record = session.execute(
                    select(PortfolioEquitySnapshotRecord).where(PortfolioEquitySnapshotRecord.bucket_start == bucket)
                ).scalar_one_or_none()
                if record is None:
                    record = PortfolioEquitySnapshotRecord(
                        id=str(uuid.uuid4()),
                        bucket_start=bucket,
                        observed_at=observed,
                        total_equity=equity,
                        available_margin=available,
                        unrealized_pnl=pnl,
                        synced_venues=synced_venues,
                    )
                    session.add(record)
                else:
                    record.observed_at = observed
                    record.total_equity = equity
                    record.available_margin = available
                    record.unrealized_pnl = pnl
                    record.synced_venues = synced_venues
                return self._portfolio_snapshot(record)

    def portfolio_equity_history(self, *, start_at: datetime) -> list[PortfolioEquitySnapshot]:
        """Read chronologically ordered real account samples from ``start_at``."""
        start = _as_utc(start_at, "start_at")
        with self._sessions() as session:
            records = session.execute(
                select(PortfolioEquitySnapshotRecord)
                .where(PortfolioEquitySnapshotRecord.bucket_start >= start)
                .order_by(PortfolioEquitySnapshotRecord.bucket_start.asc())
            ).scalars().all()
        return [self._portfolio_snapshot(record) for record in records]

    def store_order_intent(self, intent: OrderIntent) -> str:
        """Persist a hashed-token intent; it remains unusable until confirmed."""
        with self._sessions() as session:
            try:
                with session.begin():
                    if session.get(OrderIntentRecord, intent.id) is not None:
                        raise RepositoryConflict("order intent id already exists")
                    session.add(
                        OrderIntentRecord(
                            id=intent.id,
                            user_id=intent.user_id,
                            exchange_account_id=intent.exchange_account_id,
                            instrument_id=intent.instrument_id,
                            venue=intent.venue.value,
                            instrument_key=intent.instrument_key,
                            side=intent.side.value,
                            intent=intent.intent.value,
                            mode=intent.mode.value,
                            notional=intent.notional,
                            confirmation_token_hash=intent.confirmation_token_hash,
                            request_fingerprint=intent.request_fingerprint,
                            status=intent.status.value,
                            created_at=intent.created_at,
                            expires_at=intent.expires_at,
                            confirmed_at=intent.confirmed_at,
                            consumed_at=None,
                        )
                    )
                return intent.id
            except IntegrityError as error:
                raise RepositoryConflict("order intent conflicts with an existing record") from error

    def confirm_order_intent(self, intent_id: str, confirmation_token: str, *, now: datetime | None = None) -> None:
        """Atomically verify the raw token in memory and mark an intent confirmed."""
        now = _as_utc(now, "now")
        token_digest = confirmation_token_hash(confirmation_token)
        failure: str | None = None
        with self._sessions() as session:
            with session.begin():
                record = session.execute(
                    select(OrderIntentRecord).where(OrderIntentRecord.id == intent_id).with_for_update()
                ).scalar_one_or_none()
                if record is None:
                    failure = "order intent was not found"
                elif record.status != IntentStatus.PENDING_CONFIRMATION.value:
                    failure = "order intent is not waiting for confirmation"
                elif _as_utc(record.expires_at, "expires_at") <= now:
                    record.status = IntentStatus.EXPIRED.value
                    failure = "order intent expired"
                elif not hmac.compare_digest(record.confirmation_token_hash, token_digest):
                    failure = "order intent confirmation token is invalid"
                else:
                    record.status = IntentStatus.CONFIRMED.value
                    record.confirmed_at = now
        if failure:
            raise OrderIntentNotConsumable(failure)

    def consume_confirmed_order_intent(self, intent_id: str, *, now: datetime | None = None) -> ConsumedOrderIntent:
        """Lock and consume a confirmed intent exactly once, before exchange I/O."""
        now = _as_utc(now, "now")
        failure: str | None = None
        consumed: ConsumedOrderIntent | None = None
        with self._sessions() as session:
            with session.begin():
                # On PostgreSQL this is a row lock.  The status change occurs
                # before the transaction commits, so duplicate execution calls
                # block then observe ``consumed`` rather than both proceeding.
                record = session.execute(
                    select(OrderIntentRecord).where(OrderIntentRecord.id == intent_id).with_for_update()
                ).scalar_one_or_none()
                if record is None:
                    failure = "order intent was not found"
                elif record.status != IntentStatus.CONFIRMED.value:
                    failure = "order intent has already been consumed or is not confirmed"
                elif _as_utc(record.expires_at, "expires_at") <= now:
                    record.status = IntentStatus.EXPIRED.value
                    failure = "order intent expired"
                else:
                    record.status = IntentStatus.CONSUMED.value
                    record.consumed_at = now
                    consumed = ConsumedOrderIntent(
                        id=record.id,
                        user_id=record.user_id,
                        exchange_account_id=record.exchange_account_id,
                        instrument_id=record.instrument_id,
                        venue=record.venue,
                        instrument_key=record.instrument_key,
                        side=record.side,
                        intent=record.intent,
                        mode=record.mode,
                        notional=_decimal(record.notional, "notional", positive=True),
                        request_fingerprint=record.request_fingerprint,
                        expires_at=_as_utc(record.expires_at, "expires_at"),
                        consumed_at=now,
                    )
        if failure:
            raise OrderIntentNotConsumable(failure)
        if consumed is None:  # Defensive: impossible unless a DB driver violates transaction semantics.
            raise RepositoryConflict("order intent consumption produced no result")
        return consumed

    def claim_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_fingerprint: str,
        now: datetime | None = None,
        expires_in_seconds: int = 15 * 60,
    ) -> IdempotencyClaim:
        """Persistently claim a request key.  Only ``claimed=True`` may execute."""
        now = _as_utc(now, "now")
        domain_record = IdempotencyKey.claim(
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
            now=now,
            expires_in_seconds=expires_in_seconds,
        )
        with self._sessions() as session:
            try:
                with session.begin():
                    existing = session.execute(
                        select(IdempotencyKeyRecord)
                        .where(
                            IdempotencyKeyRecord.scope == domain_record.scope,
                            IdempotencyKeyRecord.key_hash == domain_record.key_hash,
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    if existing is not None:
                        if _as_utc(existing.expires_at, "expires_at") > now:
                            return IdempotencyClaim(
                                id=existing.id,
                                claimed=False,
                                status=existing.status,
                                response_reference=existing.response_reference,
                                expires_at=_as_utc(existing.expires_at, "expires_at"),
                            )
                        # Reclaiming an expired key reuses only its hash, never the raw key.
                        existing.request_fingerprint = domain_record.request_fingerprint
                        existing.status = "claimed"
                        existing.response_reference = None
                        existing.created_at = now
                        existing.expires_at = domain_record.expires_at
                        return IdempotencyClaim(
                            id=existing.id,
                            claimed=True,
                            status=existing.status,
                            response_reference=None,
                            expires_at=_as_utc(existing.expires_at, "expires_at"),
                        )
                    # ``begin_nested`` means a simultaneous insert which wins the
                    # unique constraint does not abort the outer transaction.
                    try:
                        with session.begin_nested():
                            session.add(
                                IdempotencyKeyRecord(
                                    id=domain_record.id,
                                    scope=domain_record.scope,
                                    key_hash=domain_record.key_hash,
                                    request_fingerprint=domain_record.request_fingerprint,
                                    status=domain_record.status,
                                    response_reference=None,
                                    created_at=domain_record.created_at,
                                    expires_at=domain_record.expires_at,
                                )
                            )
                            session.flush()
                    except IntegrityError:
                        existing = session.execute(
                            select(IdempotencyKeyRecord).where(
                                IdempotencyKeyRecord.scope == domain_record.scope,
                                IdempotencyKeyRecord.key_hash == domain_record.key_hash,
                            )
                        ).scalar_one_or_none()
                        if existing is None:
                            raise RepositoryConflict("idempotency claim conflicted but could not be read")
                        return IdempotencyClaim(
                            id=existing.id,
                            claimed=False,
                            status=existing.status,
                            response_reference=existing.response_reference,
                            expires_at=_as_utc(existing.expires_at, "expires_at"),
                        )
                    return IdempotencyClaim(
                        id=domain_record.id,
                        claimed=True,
                        status=domain_record.status,
                        response_reference=None,
                        expires_at=domain_record.expires_at,
                    )
            except IntegrityError as error:
                raise RepositoryConflict("idempotency key conflicts with an existing record") from error

    def complete_idempotency(
        self,
        claim_id: str,
        *,
        status: str = "completed",
        response_reference: str | None = None,
    ) -> IdempotencyClaim:
        status = _public_reference(status, "idempotency status")
        response_reference = _public_reference(response_reference, "response reference")
        with self._sessions() as session:
            with session.begin():
                record = session.execute(
                    select(IdempotencyKeyRecord).where(IdempotencyKeyRecord.id == claim_id).with_for_update()
                ).scalar_one_or_none()
                if record is None:
                    raise RecordNotFound("idempotency claim was not found")
                record.status = status
                record.response_reference = response_reference
                return IdempotencyClaim(
                    id=record.id,
                    claimed=False,
                    status=record.status,
                    response_reference=record.response_reference,
                    expires_at=_as_utc(record.expires_at, "expires_at"),
                )

    def store_order(self, order: Order) -> OrderStateSnapshot:
        """Persist the pre-exchange order record and its initial audit state."""
        with self._sessions() as session:
            try:
                with session.begin():
                    intent = session.execute(
                        select(OrderIntentRecord).where(OrderIntentRecord.id == order.order_intent_id).with_for_update()
                    ).scalar_one_or_none()
                    if intent is None:
                        raise RecordNotFound("parent order intent was not found")
                    if intent.status != IntentStatus.CONSUMED.value:
                        raise OrderIntentNotConsumable("parent order intent must be consumed before order persistence")
                    if (
                        intent.exchange_account_id != order.exchange_account_id
                        or intent.instrument_id != order.instrument_id
                        or intent.venue != order.venue.value
                        or intent.instrument_key != order.instrument_key
                    ):
                        raise RepositoryConflict("order does not match its consumed intent")
                    if session.get(OrderRecord, order.id) is not None:
                        raise RepositoryConflict("order id already exists")
                    record = OrderRecord(
                        id=order.id,
                        order_intent_id=order.order_intent_id,
                        exchange_account_id=order.exchange_account_id,
                        instrument_id=order.instrument_id,
                        venue=order.venue.value,
                        instrument_key=order.instrument_key,
                        client_order_id=order.client_order_id,
                        exchange_order_id=order.exchange_order_id,
                        side=order.side.value,
                        intent=order.intent.value,
                        mode=order.mode.value,
                        quantity=order.quantity,
                        limit_price=order.limit_price,
                        filled_quantity=order.filled_quantity,
                        reduce_only=order.reduce_only,
                        status=order.status.value,
                        created_at=order.created_at,
                        updated_at=order.updated_at,
                    )
                    session.add(record)
                    self._append_audit_row(
                        session,
                        AuditEvent(
                            id=str(uuid.uuid4()),
                            event_type="order.created",
                            success=True,
                            venue=order.venue,
                            instrument_key=order.instrument_key,
                            order_id=order.id,
                            metadata={"status": order.status.value, "client_order_id": order.client_order_id},
                        ),
                    )
                    return self._order_snapshot(record)
            except IntegrityError as error:
                raise RepositoryConflict("order conflicts with an existing durable record") from error

    def record_order_status(
        self,
        order_id: str,
        status: OrderStatus,
        *,
        exchange_order_id: str | None = None,
        filled_quantity: Decimal | None = None,
        now: datetime | None = None,
        audit_metadata: Mapping[str, Any] | None = None,
    ) -> OrderStateSnapshot:
        """Lock an order, validate its transition, update it, and audit it atomically."""
        now = _as_utc(now, "now")
        target = OrderStatus(status).value
        with self._sessions() as session:
            with session.begin():
                record = session.execute(
                    select(OrderRecord).where(OrderRecord.id == order_id).with_for_update()
                ).scalar_one_or_none()
                if record is None:
                    raise RecordNotFound("order was not found")
                current = record.status
                if target not in _ORDER_TRANSITIONS.get(current, set()):
                    raise OrderStateTransitionError(f"cannot transition order from {current} to {target}")
                if exchange_order_id is not None:
                    record.exchange_order_id = _public_reference(exchange_order_id, "exchange order id")
                if filled_quantity is not None:
                    quantity = _decimal(filled_quantity, "filled quantity")
                    if quantity < Decimal(str(record.filled_quantity)) or quantity > Decimal(str(record.quantity)):
                        raise OrderStateTransitionError("filled quantity must be monotonic and cannot exceed quantity")
                    record.filled_quantity = quantity
                if target == OrderStatus.FILLED.value and Decimal(str(record.filled_quantity)) < Decimal(str(record.quantity)):
                    raise OrderStateTransitionError("filled orders require a full filled quantity")
                record.status = target
                record.updated_at = now
                metadata = {"from_status": current, "to_status": target}
                if audit_metadata:
                    metadata["detail"] = dict(audit_metadata)
                self._append_audit_row(
                    session,
                    AuditEvent(
                        id=str(uuid.uuid4()),
                        event_type="order.status_changed",
                        success=target not in {OrderStatus.REJECTED.value, OrderStatus.UNKNOWN.value},
                        venue=record.venue,
                        instrument_key=record.instrument_key,
                        order_id=record.id,
                        metadata=metadata,
                        occurred_at=now,
                    ),
                )
                return self._order_snapshot(record)

    def append_audit(self, event: AuditEvent) -> str:
        """Append a pre-redacted domain audit event without returning metadata."""
        with self._sessions() as session:
            with session.begin():
                self._append_audit_row(session, event)
        return event.id

    def bootstrap_single_operator(
        self,
        *,
        user: User,
        account: ExchangeAccount,
        instruments: Iterable[Instrument],
    ) -> BootstrapMapping:
        """Upsert safe public mapping data for one local operator account.

        ``ExchangeAccount`` rejects raw credentials itself.  This method stores
        only its safe credential reference and never returns that reference.
        """
        if account.user_id != user.id:
            raise DomainValidationError("bootstrap account must belong to the bootstrap user")
        instrument_list = list(instruments)
        if any(instrument.venue is not account.venue for instrument in instrument_list):
            raise DomainValidationError("bootstrap instruments must belong to the account venue")
        with self._sessions() as session:
            try:
                with session.begin():
                    self._upsert_user(session, user)
                    self._upsert_account(session, account)
                    mapping: dict[str, str] = {}
                    for instrument in instrument_list:
                        mapping[instrument.canonical_key] = self._upsert_instrument(session, instrument)
                    return BootstrapMapping(
                        user_id=user.id,
                        exchange_account_id=account.id,
                        instrument_ids=mapping,
                    )
            except IntegrityError as error:
                raise RepositoryConflict("bootstrap mapping conflicts with existing records") from error

    @staticmethod
    def _intent_record_to_consumed(record: Any, consumed_at: datetime) -> ConsumedOrderIntent:
        return ConsumedOrderIntent(
            id=record.id,
            user_id=record.user_id,
            exchange_account_id=record.exchange_account_id,
            instrument_id=record.instrument_id,
            venue=record.venue,
            instrument_key=record.instrument_key,
            side=record.side,
            intent=record.intent,
            mode=record.mode,
            notional=_decimal(record.notional, "notional", positive=True),
            request_fingerprint=record.request_fingerprint,
            expires_at=_as_utc(record.expires_at, "expires_at"),
            consumed_at=consumed_at,
        )

    @staticmethod
    def _order_snapshot(record: Any) -> OrderStateSnapshot:
        return OrderStateSnapshot(
            id=record.id,
            order_intent_id=record.order_intent_id,
            status=record.status,
            exchange_order_id=record.exchange_order_id,
            filled_quantity=_decimal(record.filled_quantity, "filled_quantity"),
            updated_at=_as_utc(record.updated_at, "updated_at"),
        )

    @staticmethod
    def _portfolio_snapshot(record: Any) -> PortfolioEquitySnapshot:
        return PortfolioEquitySnapshot(
            bucket_start=_as_utc(record.bucket_start, "bucket_start"),
            observed_at=_as_utc(record.observed_at, "observed_at"),
            total_equity=_decimal(record.total_equity, "total_equity"),
            available_margin=_decimal(record.available_margin, "available_margin"),
            unrealized_pnl=_signed_decimal(record.unrealized_pnl, "unrealized_pnl"),
            synced_venues=int(record.synced_venues),
        )

    @staticmethod
    def _append_audit_row(session: Any, event: AuditEvent) -> None:
        record = event.to_log_record()
        session.add(
            AuditEventRecord(
                id=record["id"],
                event_type=record["event_type"],
                success=record["success"],
                actor_user_id=record["actor_user_id"],
                venue=record["venue"],
                instrument_key=record["instrument_key"],
                order_id=record["order_id"],
                metadata_json=record["metadata"],
                occurred_at=_as_utc(event.occurred_at, "occurred_at"),
            )
        )

    @staticmethod
    def _upsert_user(session: Any, user: User) -> None:
        record = session.get(UserRecord, user.id)
        if record is None:
            session.add(
                UserRecord(
                    id=user.id,
                    username=user.username,
                    email=user.email,
                    role=user.role,
                    is_active=user.is_active,
                    created_at=user.created_at,
                )
            )
            return
        record.username = user.username
        record.email = user.email
        record.role = user.role
        record.is_active = user.is_active

    @staticmethod
    def _upsert_account(session: Any, account: ExchangeAccount) -> None:
        record = session.get(ExchangeAccountRecord, account.id)
        if record is None:
            session.add(
                ExchangeAccountRecord(
                    id=account.id,
                    user_id=account.user_id,
                    venue=account.venue.value,
                    label=account.label,
                    mode=account.mode.value,
                    credential_reference=account.credential_reference,
                    is_active=account.is_active,
                    created_at=account.created_at,
                )
            )
            return
        record.user_id = account.user_id
        record.venue = account.venue.value
        record.label = account.label
        record.mode = account.mode.value
        record.credential_reference = account.credential_reference
        record.is_active = account.is_active

    @staticmethod
    def _upsert_instrument(session: Any, instrument: Instrument) -> str:
        record = session.execute(
            select(InstrumentRecord)
            .where(InstrumentRecord.venue == instrument.venue.value, InstrumentRecord.external_id == instrument.external_id)
            .with_for_update()
        ).scalar_one_or_none()
        if record is None:
            session.add(
                InstrumentRecord(
                    id=instrument.id,
                    venue=instrument.venue.value,
                    external_id=instrument.external_id,
                    symbol=instrument.symbol,
                    instrument_type=instrument.instrument_type.value,
                    base_asset=instrument.base_asset,
                    quote_asset=instrument.quote_asset,
                    min_notional=instrument.min_notional,
                    quantity_step=instrument.quantity_step,
                    price_tick=instrument.price_tick,
                    is_active=instrument.is_active,
                    metadata_json=dict(instrument.metadata or {}),
                    created_at=instrument.created_at,
                )
            )
            return instrument.id
        record.symbol = instrument.symbol
        record.instrument_type = instrument.instrument_type.value
        record.base_asset = instrument.base_asset
        record.quote_asset = instrument.quote_asset
        record.min_notional = instrument.min_notional
        record.quantity_step = instrument.quantity_step
        record.price_tick = instrument.price_tick
        record.is_active = instrument.is_active
        record.metadata_json = dict(instrument.metadata or {})
        return record.id


def repository_if_configured(
    database_url: str | None,
    *,
    initialize_schema: bool = False,
) -> PersistenceRepository | None:
    """Return ``None`` when persistence is deliberately not configured.

    This helper deliberately does not read environment variables.  The runtime
    owns configuration and can leave DATABASE_URL unset to keep current local
    behavior unchanged.
    """
    if database_url is None or not database_url.strip():
        return None
    return PersistenceRepository.from_database_url(database_url, initialize_schema=initialize_schema)


__all__ = [
    "BootstrapMapping",
    "ConsumedOrderIntent",
    "IdempotencyClaim",
    "OrderIntentNotConsumable",
    "OrderStateSnapshot",
    "PortfolioEquitySnapshot",
    "OrderStateTransitionError",
    "PersistenceRepository",
    "RecordNotFound",
    "RepositoryConflict",
    "RepositoryError",
    "repository_if_configured",
]
