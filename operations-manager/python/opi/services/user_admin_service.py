"""Service layer for platform-level user management.

Provides CRUD operations for the users table (email + full name), via the SQLAlchemy
ORM (:class:`opi.services.persistence.users.User`) on the shared async engine.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, func, select, update

from opi.core.db import session_scope
from opi.services.persistence.users import User

logger = logging.getLogger(__name__)


class UserAdminService:
    """Service for managing platform users (ORM-backed)."""

    async def list_users(self) -> list[dict]:
        """List all users ordered by full_name."""
        async with session_scope() as session:
            result = await session.execute(select(User).order_by(User.full_name.asc()))
            return [row.to_dict() for row in result.scalars().all()]

    async def get_user(self, user_id: str) -> dict | None:
        """Get a user by UUID."""
        async with session_scope() as session:
            row = await session.get(User, uuid.UUID(user_id))
            return row.to_dict() if row else None

    async def get_user_by_email(self, email: str) -> dict | None:
        """Get a user by email address."""
        async with session_scope() as session:
            row = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
            return row.to_dict() if row else None

    async def create_user(self, email: str, full_name: str) -> dict:
        """Create a new user. Returns the created user dict."""
        async with session_scope() as session:
            row = User(email=email, full_name=full_name)
            session.add(row)
            await session.flush()
            await session.refresh(row)  # pick up server defaults (id, timestamps)
            logger.info("Created user '%s' (%s)", full_name, email)
            return row.to_dict()

    async def update_user(self, user_id: str, email: str, full_name: str) -> dict | None:
        """Update a user. Returns the updated user dict, or None if not found."""
        async with session_scope() as session:
            result = await session.execute(
                update(User)
                .where(User.id == uuid.UUID(user_id))
                .values(email=email, full_name=full_name, updated_at=func.now())
            )
            if result.rowcount == 0:
                return None
            row = await session.get(User, uuid.UUID(user_id))
            logger.info("Updated user %s ('%s', %s)", user_id, full_name, email)
            return row.to_dict() if row else None

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user. Returns True if deleted, False if not found."""
        async with session_scope() as session:
            result = await session.execute(delete(User).where(User.id == uuid.UUID(user_id)))
            deleted = result.rowcount > 0
            if deleted:
                logger.info("Deleted user %s", user_id)
            return deleted
