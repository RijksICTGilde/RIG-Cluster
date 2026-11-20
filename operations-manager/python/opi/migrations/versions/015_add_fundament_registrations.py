"""Add the two tables of the Fundament fieldlab registration.

Two and not one: a registration belongs to a team, the ACME account belongs to the ACME
directory. Both leave again when the fieldlab is over, see features/fundament-registratie.md.

Revision ID: 015
Revises: 014
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS fundament_registrations (
    team TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    state TEXT NOT NULL,
    step TEXT,
    certificate TEXT,
    private_key TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS fundament_acme_accounts (
    directory TEXT PRIMARY KEY,
    files TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
"""

DROP_SQL = """
DROP TABLE IF EXISTS fundament_registrations;
DROP TABLE IF EXISTS fundament_acme_accounts;
"""


def upgrade() -> None:
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
