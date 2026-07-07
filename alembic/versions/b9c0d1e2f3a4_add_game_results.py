"""Add game_results: completed games with closing lines, for trend records.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "game_results",
        sa.Column("game_id", sa.String(255), nullable=False),
        sa.Column("sport", sa.String(50), nullable=False),
        sa.Column("home_team", sa.String(100), nullable=False),
        sa.Column("away_team", sa.String(100), nullable=False),
        sa.Column("commence_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("home_score", sa.Integer(), nullable=False),
        sa.Column("away_score", sa.Integer(), nullable=False),
        sa.Column("closing_spread_home", sa.Float(), nullable=True),
        sa.Column("closing_total", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("game_id"),
    )
    op.create_index("ix_game_results_sport_home", "game_results", ["sport", "home_team"])
    op.create_index("ix_game_results_sport_away", "game_results", ["sport", "away_team"])


def downgrade() -> None:
    op.drop_index("ix_game_results_sport_away", table_name="game_results")
    op.drop_index("ix_game_results_sport_home", table_name="game_results")
    op.drop_table("game_results")
