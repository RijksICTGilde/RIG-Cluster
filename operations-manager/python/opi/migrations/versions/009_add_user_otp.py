"""Add the OTP columns on users (RC-329).

Nummer 009 en niet 008: 008 (session_revocations) is de laatste head op main.

Revision ID: 009
Revises: 008
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# IF NOT EXISTS, want USERS_TABLE_SQL (migratie 003) noemt deze kolommen nu ook: een
# verse database heeft ze dan al en een bestaande krijgt ze hier.
OTP_COLUMNS_SQL = """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS otp_secret TEXT,
    ADD COLUMN IF NOT EXISTS otp_key_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS otp_confirmed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS otp_last_used_step BIGINT,
    ADD COLUMN IF NOT EXISTS otp_failed_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS otp_locked_until TIMESTAMPTZ;
"""

DROP_OTP_COLUMNS_SQL = """
ALTER TABLE users
    DROP COLUMN IF EXISTS otp_secret,
    DROP COLUMN IF EXISTS otp_key_version,
    DROP COLUMN IF EXISTS otp_confirmed_at,
    DROP COLUMN IF EXISTS otp_last_used_step,
    DROP COLUMN IF EXISTS otp_failed_attempts,
    DROP COLUMN IF EXISTS otp_locked_until;
"""


def upgrade() -> None:
    op.execute(OTP_COLUMNS_SQL)


def downgrade() -> None:
    op.execute(DROP_OTP_COLUMNS_SQL)
