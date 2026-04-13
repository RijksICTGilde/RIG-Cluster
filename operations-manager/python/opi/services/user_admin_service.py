"""Service layer for platform-level user management.

Provides CRUD operations for the users table (email + full name).
"""

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.core.database_pool import DatabasePool

logger = logging.getLogger(__name__)


def _row_to_dict(row) -> dict | None:
    """Convert an asyncpg Record to a dict with serializable types."""
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


class UserAdminService:
    """Service for managing platform users."""

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def list_users(self) -> list[dict]:
        """List all users ordered by full_name."""
        conn = await self._pool.acquire()
        try:
            rows = await conn.fetch("SELECT * FROM users ORDER BY full_name ASC")
            return [_row_to_dict(row) for row in rows]
        finally:
            await self._pool.release(conn)

    async def get_user(self, user_id: str) -> dict | None:
        """Get a user by UUID."""
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", uuid.UUID(user_id))
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def get_user_by_email(self, email: str) -> dict | None:
        """Get a user by email address."""
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow("SELECT * FROM users WHERE email = $1", email)
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def create_user(self, email: str, full_name: str) -> dict:
        """Create a new user. Returns the created user dict."""
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO users (email, full_name)
                VALUES ($1, $2)
                RETURNING *
                """,
                email,
                full_name,
            )
            logger.info("Created user '%s' (%s)", full_name, email)
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def update_user(self, user_id: str, email: str, full_name: str) -> dict | None:
        """Update a user. Returns the updated user dict, or None if not found."""
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow(
                """
                UPDATE users
                SET email = $2, full_name = $3, updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                uuid.UUID(user_id),
                email,
                full_name,
            )
            if row:
                logger.info("Updated user %s ('%s', %s)", user_id, full_name, email)
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user. Returns True if deleted, False if not found."""
        conn = await self._pool.acquire()
        try:
            result = await conn.execute("DELETE FROM users WHERE id = $1", uuid.UUID(user_id))
            deleted_count = int(result.split()[-1])
            if deleted_count > 0:
                logger.info("Deleted user %s", user_id)
            return deleted_count > 0
        finally:
            await self._pool.release(conn)
