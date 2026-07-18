"""Add nhl_player_games: per-game skater stat lines for NHL prop analysis.

Revision ID: d2e3f4a5b6c7
Revises: c7d8e9f0a1b2
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nhl_player_games",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("player", sa.String(100), nullable=False),
        sa.Column("team", sa.String(100), nullable=False),
        sa.Column("opponent", sa.String(100), nullable=False),
        sa.Column("opposing_goalie", sa.String(100), nullable=True),
        sa.Column("is_home", sa.Boolean(), nullable=False),
        sa.Column("rest_days", sa.Integer(), nullable=True),
        sa.Column("goals", sa.Integer(), nullable=True),
        sa.Column("assists", sa.Integer(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=True),
        sa.Column("shots_on_goal", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_nhl_player_games_player", "nhl_player_games", ["player", "season"]
    )


def downgrade() -> None:
    op.drop_index("ix_nhl_player_games_player", table_name="nhl_player_games")
    op.drop_table("nhl_player_games")
