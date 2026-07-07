"""Add line_snapshots for steam-detection line history.

Revision ID: e6a7b8c9d0f1
Revises: d5f6a7b8c9e0
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e6a7b8c9d0f1"
down_revision: Union[str, None] = "d5f6a7b8c9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "line_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.String(255), nullable=False),
        sa.Column("book", sa.String(100), nullable=False),
        sa.Column("market", sa.String(50), nullable=False),
        sa.Column("outcome", sa.String(255), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("point", sa.Float(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_line_snapshots_lookup",
        "line_snapshots",
        ["game_id", "market", "book", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_line_snapshots_lookup", table_name="line_snapshots")
    op.drop_table("line_snapshots")
