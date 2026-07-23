"""create control plane persistence schema

Revision ID: 0001_control_plane_schema
Revises:
Create Date: 2026-07-23
"""
from __future__ import annotations

from alembic import op

from persistence import Base, require_sqlalchemy

revision = "0001_control_plane_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    require_sqlalchemy()
    # Keep this historical revision immutable in scope.  New models belong in
    # their own Alembic revision; otherwise a fresh install would create tables
    # from the newest metadata here and the next revision would fail on an
    # already-existing table.
    original_tables = (
        "users",
        "exchange_accounts",
        "instruments",
        "order_intents",
        "orders",
        "follow_strategies",
        "idempotency_keys",
        "audit_events",
    )
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in original_tables])


def downgrade() -> None:
    require_sqlalchemy()
    original_tables = (
        "audit_events",
        "idempotency_keys",
        "follow_strategies",
        "orders",
        "order_intents",
        "instruments",
        "exchange_accounts",
        "users",
    )
    Base.metadata.drop_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in original_tables])
