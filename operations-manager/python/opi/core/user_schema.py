"""SQL schema for the users table.

The USERS_TABLE_SQL constant is used by the Alembic migration
(opi/migrations/versions/003_add_users.py) to create the table.

This table stores platform-level users with email and full name,
managed via the admin UI.
"""

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
"""
