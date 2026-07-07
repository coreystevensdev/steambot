"""Add users.api_key_hash for API key auth.

Revision ID: c4e5f6a7b8d9
Revises: b7d8e9f0a1c2
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4e5f6a7b8d9"
down_revision: Union[str, None] = "b7d8e9f0a1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("api_key_hash", sa.String(64), nullable=True))
    # unique index, not a constraint: SQLite cannot ALTER constraints in place
    op.create_index("ix_users_api_key_hash", "users", ["api_key_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_api_key_hash", table_name="users")
    op.drop_column("users", "api_key_hash")
