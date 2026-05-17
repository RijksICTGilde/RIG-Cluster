"""Add users table for platform-level user management.

Revision ID: 003
Revises: 002
Create Date: 2026-03-19
"""

from collections.abc import Sequence

from alembic import op
from opi.core.user_schema import USERS_TABLE_SQL

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(USERS_TABLE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS users;")
