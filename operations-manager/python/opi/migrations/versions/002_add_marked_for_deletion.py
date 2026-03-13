"""Add marked_for_deletion table for deferred resource cleanup.

Revision ID: 002
Revises: 001
Create Date: 2026-03-04
"""

from collections.abc import Sequence

from alembic import op
from opi.core.marked_for_deletion_schema import MARKED_FOR_DELETION_TABLE_SQL

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(MARKED_FOR_DELETION_TABLE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS marked_for_deletion;")
