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
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    require_sqlalchemy()
    Base.metadata.drop_all(bind=op.get_bind())
