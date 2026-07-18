"""Add nfl player_games context columns: home/away, surface, primetime, weather.

Revision ID: c7d8e9f0a1b2
Revises: b3c4d5e6f7a8
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("player_games", sa.Column("is_home", sa.Boolean(), nullable=True))
    op.add_column("player_games", sa.Column("surface", sa.String(30), nullable=True))
    op.add_column("player_games", sa.Column("is_primetime", sa.Boolean(), nullable=True))
    op.add_column("player_games", sa.Column("bad_weather", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("player_games", "bad_weather")
    op.drop_column("player_games", "is_primetime")
    op.drop_column("player_games", "surface")
    op.drop_column("player_games", "is_home")
