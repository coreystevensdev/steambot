"""Add picks.sim_probability for the sim blend and its CLV validation.

Revision ID: d5f6a7b8c9e0
Revises: c4e5f6a7b8d9
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d5f6a7b8c9e0"
down_revision: Union[str, None] = "c4e5f6a7b8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("sim_probability", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("picks", "sim_probability")
