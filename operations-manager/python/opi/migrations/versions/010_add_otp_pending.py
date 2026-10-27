"""Add otp_pending_registrations: de lopende registratie van een tweede factor (server-side).

Vervangt de vingerafdruk-in-de-sessiecookie van RC-329: op een sessie die al een id_token
droeg liep die erover heen (browsers laten >4 KB cookies geruisloos vallen) en de
bevestiging strandde zonder melding. Server-side zoals AMT/TAD dat deed, met TTL.

Nummer 010 en volgt op 009 (de OTP-kolommen op users), laatste head op main toen dit
geschreven werd.

Revision ID: 010
Revises: 009
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS otp_pending_registrations (
    email TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
"""

DROP_SQL = "DROP TABLE IF EXISTS otp_pending_registrations;"


def upgrade() -> None:
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
