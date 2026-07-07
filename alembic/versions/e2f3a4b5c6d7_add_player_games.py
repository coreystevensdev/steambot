"""Add player_games: per-game stat lines for prop analysis and grading.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_games",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sport", sa.String(50), nullable=False, server_default="americanfootball_nfl"),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("player", sa.String(100), nullable=False),
        sa.Column("team", sa.String(100), nullable=False),
        sa.Column("opponent", sa.String(100), nullable=False),
        sa.Column("passing_yards", sa.Float(), nullable=True),
        sa.Column("rushing_yards", sa.Float(), nullable=True),
        sa.Column("receiving_yards", sa.Float(), nullable=True),
        sa.Column("receptions", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_player_games_player", "player_games", ["sport", "player", "season", "week"]
    )


def downgrade() -> None:
    op.drop_index("ix_player_games_player", table_name="player_games")
    op.drop_table("player_games")
