"""SQL schema for the users table.

The USERS_TABLE_SQL constant is used by the Alembic migration
(opi/migrations/versions/003_add_users.py) to create the table.

This table stores platform-level users with email and full name,
managed via the admin UI, plus the OTP columns of their second factor (RC-329).
Migration 009 adds those columns to a database that was created before them, so
both sides use IF NOT EXISTS and stay idempotent.
"""

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    otp_secret TEXT,
    otp_key_version INTEGER NOT NULL DEFAULT 1,
    otp_confirmed_at TIMESTAMPTZ,
    otp_last_used_step BIGINT,
    otp_failed_attempts INTEGER NOT NULL DEFAULT 0,
    otp_locked_until TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
"""
