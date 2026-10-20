"""Add session_revocations: from when a user's portal sessions are invalid (RC-287).

Revision ID: 008
Revises: 007
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_revocations (
            email TEXT PRIMARY KEY,
            revoked_after TIMESTAMPTZ NOT NULL
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_revocations;")
