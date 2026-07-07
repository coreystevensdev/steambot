"""Add steam_candidates: lagging retail prices awaiting approval.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "steam_candidates",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sport", sa.String(50), nullable=False),
        sa.Column("game_id", sa.String(255), nullable=False),
        sa.Column("home_team", sa.String(100), nullable=False),
        sa.Column("away_team", sa.String(100), nullable=False),
        sa.Column("commence_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("market", sa.String(50), nullable=False),
        sa.Column("selection", sa.String(255), nullable=False),
        sa.Column("book", sa.String(100), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("sharp_probability", sa.Float(), nullable=False),
        sa.Column("implied_probability", sa.Float(), nullable=False),
        sa.Column("edge_pct", sa.Float(), nullable=False),
        sa.Column("ev_pct", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_steam_candidates_status", "steam_candidates", ["status"])


def downgrade() -> None:
    op.drop_index("ix_steam_candidates_status", table_name="steam_candidates")
    op.drop_table("steam_candidates")
