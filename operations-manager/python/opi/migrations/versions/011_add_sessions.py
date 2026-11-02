"""Add sessions: de server-side sessie-inhoud achter het ondertekende sessie-id.

Vanaf RC-390 draagt de sessiecookie alleen een ondertekend id; de inhoud woont hier, dus
de cookie kan niet meer groeien. ``expires_at`` is het schuivende venster van de
inactiviteitsgrens (``SESSION_MAX_AGE_SECONDS``); de absolute grens zit in de data
(``user.login_at``, ``opi/core/session_guard.py``). De uurlijkse lus van de taakwerker
ruimt verlopen rijen op (``SessionStore.purge_expired``).

Revision ID: 011
Revises: 010
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL
);
"""

DROP_SQL = "DROP TABLE IF EXISTS sessions;"


def upgrade() -> None:
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
