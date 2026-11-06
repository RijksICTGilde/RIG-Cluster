"""Add api_step_ups: the step-up of an API credential, like step_up_at in a portal session.

Revision ID: 014
Revises: 013
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS api_step_ups (
    credential TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    stepped_up_at TIMESTAMPTZ NOT NULL
);
"""

DROP_SQL = "DROP TABLE IF EXISTS api_step_ups;"


def upgrade() -> None:
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
