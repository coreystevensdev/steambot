"""Add steam_candidates.source: the review queue now serves multiple agents.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "steam_candidates",
        sa.Column("source", sa.String(20), nullable=False, server_default="steam"),
    )


def downgrade() -> None:
    op.drop_column("steam_candidates", "source")
