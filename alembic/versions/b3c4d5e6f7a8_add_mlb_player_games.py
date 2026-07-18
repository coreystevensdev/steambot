"""Add mlb_player_games: per-game batter stat lines for MLB prop analysis.

Revision ID: b3c4d5e6f7a8
Revises: a4b5c6d7e8f9
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mlb_player_games",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("player", sa.String(100), nullable=False),
        sa.Column("team", sa.String(100), nullable=False),
        sa.Column("opponent", sa.String(100), nullable=False),
        sa.Column("opposing_pitcher", sa.String(100), nullable=True),
        sa.Column("is_home", sa.Boolean(), nullable=False),
        sa.Column("day_night", sa.String(10), nullable=False),
        sa.Column("at_bats", sa.Integer(), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=True),
        sa.Column("home_runs", sa.Integer(), nullable=True),
        sa.Column("rbis", sa.Integer(), nullable=True),
        sa.Column("total_bases", sa.Integer(), nullable=True),
        sa.Column("strikeouts", sa.Integer(), nullable=True),
        sa.Column("walks", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mlb_player_games_player", "mlb_player_games", ["player", "season"]
    )


def downgrade() -> None:
    op.drop_index("ix_mlb_player_games_player", table_name="mlb_player_games")
    op.drop_table("mlb_player_games")
