"""Add runs registry table for user-launched ephemeral workloads.

Revision ID: 004
Revises: 003
Create Date: 2026-06-27
"""

from collections.abc import Sequence

from alembic import op
from opi.core.runs_schema import RUNS_TABLE_SQL

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(RUNS_TABLE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS runs;")
