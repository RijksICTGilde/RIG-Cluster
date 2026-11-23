"""Add routing_mode: the routing stand OPI adopted, so one switch reprocesses once.

Revision ID: 016
Revises: 015
Create Date: 2026-11-20
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS routing_mode (
    cluster TEXT PRIMARY KEY,
    routing TEXT NOT NULL,
    adopted_at TIMESTAMPTZ NOT NULL
);
"""

DROP_SQL = "DROP TABLE IF EXISTS routing_mode;"


def upgrade() -> None:
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
