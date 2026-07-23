"""Optional SQLAlchemy persistence schema for the trading control plane.

This module has no startup side effects and never reads ``.env``.  The main
service may import it later and call :func:`initialize_schema` during an
explicit migration/bootstrap step.  SQLAlchemy is intentionally optional so
the existing lightweight local runner keeps working until a production database
is deliberately enabled.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

try:  # Supports both ``import persistence`` and ``import backend.persistence``.
    from .domain_models import utc_now
except ImportError:  # pragma: no cover - exercised by direct script imports
    from domain_models import utc_now


SCHEMA_TABLES: tuple[str, ...] = (
    "users",
    "exchange_accounts",
    "instruments",
    "order_intents",
    "orders",
    "follow_strategies",
    "idempotency_keys",
    "audit_events",
    "portfolio_equity_snapshots",
)


class PersistenceDependencyError(RuntimeError):
    """Raised only when database persistence is explicitly requested."""


try:
    from sqlalchemy import (
        JSON,
        Boolean,
        DateTime,
        ForeignKey,
        Index,
        Integer,
        Numeric,
        String,
        Text,
        UniqueConstraint,
        create_engine,
    )
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
except ImportError as error:  # Keep the original project dependency-free by default.
    SQLALCHEMY_AVAILABLE = False
    SQLALCHEMY_IMPORT_ERROR: ImportError | None = error
    Base: Any = None
else:
    SQLALCHEMY_AVAILABLE = True
    SQLALCHEMY_IMPORT_ERROR = None

    class Base(DeclarativeBase):
        """Base class deliberately owned by this module, not application startup."""

    class UserRecord(Base):
        __tablename__ = "users"

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
        email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
        role: Mapped[str] = mapped_column(String(32), nullable=False, default="operator")
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    class ExchangeAccountRecord(Base):
        __tablename__ = "exchange_accounts"
        __table_args__ = (UniqueConstraint("user_id", "venue", "label", name="uq_exchange_account_owner_venue_label"),)

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
        venue: Mapped[str] = mapped_column(String(24), nullable=False)
        label: Mapped[str] = mapped_column(String(80), nullable=False)
        mode: Mapped[str] = mapped_column(String(24), nullable=False)
        # A secret-manager pointer only.  Never store an API secret/private key here.
        credential_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    class InstrumentRecord(Base):
        __tablename__ = "instruments"
        __table_args__ = (UniqueConstraint("venue", "external_id", name="uq_instrument_venue_external_id"),)

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        venue: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
        external_id: Mapped[str] = mapped_column(String(128), nullable=False)
        symbol: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
        instrument_type: Mapped[str] = mapped_column(String(24), nullable=False, default="perpetual")
        base_asset: Mapped[str | None] = mapped_column(String(24), nullable=True)
        quote_asset: Mapped[str | None] = mapped_column(String(24), nullable=True)
        min_notional: Mapped[Any | None] = mapped_column(Numeric(38, 18), nullable=True)
        quantity_step: Mapped[Any | None] = mapped_column(Numeric(38, 18), nullable=True)
        price_tick: Mapped[Any | None] = mapped_column(Numeric(38, 18), nullable=True)
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
        metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    class OrderIntentRecord(Base):
        __tablename__ = "order_intents"
        __table_args__ = (UniqueConstraint("confirmation_token_hash", name="uq_order_intent_confirmation_token_hash"),)

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
        exchange_account_id: Mapped[str] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False, index=True)
        instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True)
        venue: Mapped[str] = mapped_column(String(24), nullable=False)
        instrument_key: Mapped[str] = mapped_column(String(180), nullable=False)
        side: Mapped[str] = mapped_column(String(8), nullable=False)
        intent: Mapped[str] = mapped_column(String(8), nullable=False)
        mode: Mapped[str] = mapped_column(String(16), nullable=False)
        notional: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False)
        # Hash only; this table must never contain the one-time raw token.
        confirmation_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
        request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
        status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_confirmation")
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
        expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
        confirmed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
        consumed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)

    class OrderRecord(Base):
        __tablename__ = "orders"
        __table_args__ = (
            UniqueConstraint("exchange_account_id", "client_order_id", name="uq_order_account_client_order_id"),
            UniqueConstraint("exchange_account_id", "exchange_order_id", name="uq_order_account_exchange_order_id"),
        )

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        order_intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.id", ondelete="RESTRICT"), nullable=False, index=True)
        exchange_account_id: Mapped[str] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False, index=True)
        instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True)
        venue: Mapped[str] = mapped_column(String(24), nullable=False)
        instrument_key: Mapped[str] = mapped_column(String(180), nullable=False)
        client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
        exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
        side: Mapped[str] = mapped_column(String(8), nullable=False)
        intent: Mapped[str] = mapped_column(String(8), nullable=False)
        mode: Mapped[str] = mapped_column(String(16), nullable=False)
        quantity: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False)
        limit_price: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False)
        filled_quantity: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False, default=0)
        reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
        status: Mapped[str] = mapped_column(String(32), nullable=False, default="created", index=True)
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
        updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    class FollowStrategyRecord(Base):
        __tablename__ = "follow_strategies"
        __table_args__ = (Index("ix_follow_strategy_active_market", "status", "venue", "instrument_key"),)

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
        exchange_account_id: Mapped[str] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False, index=True)
        instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True)
        venue: Mapped[str] = mapped_column(String(24), nullable=False)
        instrument_key: Mapped[str] = mapped_column(String(180), nullable=False)
        side: Mapped[str] = mapped_column(String(8), nullable=False)
        intent: Mapped[str] = mapped_column(String(8), nullable=False)
        quantity: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False)
        reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
        max_reprices_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
        status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
        active_order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
        last_price: Mapped[Any | None] = mapped_column(Numeric(38, 18), nullable=True)
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
        updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    class IdempotencyKeyRecord(Base):
        __tablename__ = "idempotency_keys"
        __table_args__ = (UniqueConstraint("scope", "key_hash", name="uq_idempotency_scope_key_hash"),)

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        scope: Mapped[str] = mapped_column(String(128), nullable=False)
        key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
        request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
        status: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed")
        response_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
        expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    class AuditEventRecord(Base):
        __tablename__ = "audit_events"
        __table_args__ = (Index("ix_audit_events_occurred_type", "occurred_at", "event_type"),)

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        event_type: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
        success: Mapped[bool] = mapped_column(Boolean, nullable=False)
        actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
        venue: Mapped[str | None] = mapped_column(String(24), nullable=True)
        instrument_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
        order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
        metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
        occurred_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    class PortfolioEquitySnapshotRecord(Base):
        """A compact, credential-free account-equity time series.

        Values are intentionally stored only after adapters have returned
        verified account data.  ``bucket_start`` makes repeated page refreshes
        update one five-minute sample instead of creating write amplification.
        """

        __tablename__ = "portfolio_equity_snapshots"
        __table_args__ = (UniqueConstraint("bucket_start", name="uq_portfolio_equity_snapshot_bucket"),)

        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        bucket_start: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
        observed_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
        total_equity: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False)
        available_margin: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False)
        unrealized_pnl: Mapped[Any] = mapped_column(Numeric(38, 18), nullable=False)
        synced_venues: Mapped[int] = mapped_column(Integer, nullable=False)
        created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


def require_sqlalchemy() -> None:
    if not SQLALCHEMY_AVAILABLE:
        raise PersistenceDependencyError(
            "SQLAlchemy is optional and is not installed. Install a pinned SQLAlchemy "
            "version only when the production persistence adapter is enabled."
        ) from SQLALCHEMY_IMPORT_ERROR


def create_database_engine(database_url: str, *, echo: bool = False) -> "Engine":
    """Create, but do not initialize, an engine.  Callers own credentials/URLs."""
    require_sqlalchemy()
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("database_url is required")
    # Do not log database_url: a PostgreSQL URL can contain a password.
    return create_engine(database_url.strip(), future=True, echo=echo, pool_pre_ping=True)


def initialize_schema(engine: "Engine") -> None:
    """Explicitly create this module's schema; no migration runs on import."""
    require_sqlalchemy()
    Base.metadata.create_all(engine)


def make_session_factory(engine: "Engine") -> "sessionmaker[Session]":
    require_sqlalchemy()
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def schema_table_names() -> Sequence[str]:
    """Available even without SQLAlchemy, for deployment preflight checks."""
    return SCHEMA_TABLES


__all__ = [
    "SCHEMA_TABLES",
    "SQLALCHEMY_AVAILABLE",
    "SQLALCHEMY_IMPORT_ERROR",
    "PersistenceDependencyError",
    "create_database_engine",
    "initialize_schema",
    "make_session_factory",
    "require_sqlalchemy",
    "schema_table_names",
]

if SQLALCHEMY_AVAILABLE:
    __all__ += [
        "Base",
        "UserRecord",
        "ExchangeAccountRecord",
        "InstrumentRecord",
        "OrderIntentRecord",
        "OrderRecord",
        "FollowStrategyRecord",
        "IdempotencyKeyRecord",
        "AuditEventRecord",
        "PortfolioEquitySnapshotRecord",
    ]
