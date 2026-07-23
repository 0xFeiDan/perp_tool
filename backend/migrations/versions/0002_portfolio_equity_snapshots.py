"""add verified portfolio equity snapshots

Revision ID: 0002_portfolio_equity_snapshots
Revises: 0001_control_plane_schema
Create Date: 2026-07-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_portfolio_equity_snapshots"
down_revision = "0001_control_plane_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_equity_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_equity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("available_margin", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("synced_venues", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket_start", name="uq_portfolio_equity_snapshot_bucket"),
    )
    op.create_index("ix_portfolio_equity_snapshots_bucket_start", "portfolio_equity_snapshots", ["bucket_start"])


def downgrade() -> None:
    op.drop_index("ix_portfolio_equity_snapshots_bucket_start", table_name="portfolio_equity_snapshots")
    op.drop_table("portfolio_equity_snapshots")
