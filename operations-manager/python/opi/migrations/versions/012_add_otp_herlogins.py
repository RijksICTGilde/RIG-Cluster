"""Add otp_herlogins: de herlogin bij Keycloak voor het inrichten van een 2-factor (server-side).

De OIDC-state van de herlogin en de uitkomst staan in een tabel. Gebouwd toen de sessie nog in
de cookie zat; sinds RC-390 staat de sessie server-side en kan dit ook daarin (open vraag).

Nummer 012 en volgt op 011 (sessions, RC-390), laatste head op main toen dit geschreven werd.

Revision ID: 012
Revises: 011
Create Date: 2026-10-28
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS otp_herlogins (
    state TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    login_at DOUBLE PRECISION NOT NULL,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_otp_herlogins_email ON otp_herlogins (email);
"""

DROP_SQL = "DROP TABLE IF EXISTS otp_herlogins;"


def upgrade() -> None:
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
