"""Server-side sessions: the ``sessions`` table with the hot cache in front of it.

Reading goes to the cache first and to the table on a miss, which fills the cache. Writing
is write-through: ``set`` fills the cache AND upserts the row, so a mutation is visible to
the next reader at once.

Expiry lives in two places: ``expires_at`` here (the sliding inactivity window, swept by
``purge_expired`` from the task worker's hourly loop) and ``user.login_at`` in the data (the
absolute limit, enforced by ``opi/http/auth/session_guard.py``).

The hit ratio and the number of sets are logged at INFO at most once a minute, on activity:
a store without traffic has nothing to report.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from opi.core.config import settings
from opi.core.db import session_scope
from opi.core.session_cache import SessionCache
from opi.services.persistence.sessions import PortalSession

logger = logging.getLogger(__name__)

#: A touch writes the database at most once per this many seconds per session. Inside that
#: window the cache mark is enough and the row may lag behind.
TOUCH_GRACE_SECONDS = 60.0

#: How often the cache counters are logged, at most.
REPORT_INTERVAL_SECONDS = 60.0


class SessionStore:
    """The ``sessions`` row behind a session id, with the hot read-through cache in front."""

    def __init__(
        self,
        cache: SessionCache | None = None,
        session_max_age: timedelta | None = None,
    ) -> None:
        self._cache = cache if cache is not None else SessionCache()
        self._session_max_age = (
            session_max_age if session_max_age is not None else timedelta(seconds=settings.SESSION_MAX_AGE_SECONDS)
        )
        self._writes = 0
        self._next_report_at = 0.0

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """This session's data, or None when it is unknown or expired."""
        self._report_if_due()
        cached = self._cache.get(session_id)
        if cached is not None:
            return cached
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(PortalSession.data).where(
                        PortalSession.id == session_id,
                        PortalSession.expires_at > datetime.now(UTC),
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        self._cache.set(session_id, row)
        return row

    async def set(self, session_id: str, data: dict[str, Any]) -> None:
        """Write-through: fill the cache AND upsert the row with a fresh expiry."""
        self._report_if_due()
        self._writes += 1
        self._cache.set(session_id, data)
        now = datetime.now(UTC)
        expires = now + self._session_max_age
        statement = insert(PortalSession).values(
            id=session_id, data=data, created_at=now, expires_at=expires, last_seen=now
        )
        statement = statement.on_conflict_do_update(
            index_elements=[PortalSession.id],
            set_={"data": data, "expires_at": expires, "last_seen": now},
        )
        async with session_scope() as session:
            await session.execute(statement)

    async def delete(self, session_id: str) -> None:
        """Gone from both: the cache and the row."""
        self._report_if_due()
        self._cache.delete(session_id)
        async with session_scope() as session:
            await session.execute(delete(PortalSession).where(PortalSession.id == session_id))

    async def delete_by_email(self, email: str) -> int:
        """Delete every session of this address, which makes a revocation a hard refusal."""
        normalized = email.strip().lower()
        self._cache.delete_where(
            lambda data: str((data.get("user") or {}).get("email") or "").strip().lower() == normalized
        )
        async with session_scope() as session:
            result = await session.execute(
                delete(PortalSession).where(func.lower(PortalSession.data["user"]["email"].as_string()) == normalized)
            )
        return result.rowcount or 0

    async def touch(self, session_id: str) -> bool:
        """Extend this session's expiry; True when a row was extended.

        An expired or absent row is NOT revived, so touch cannot keep a session alive
        indefinitely. Within :data:`TOUCH_GRACE_SECONDS` of the previous touch the cache mark
        is enough and the database stays out of the way.
        """
        self._report_if_due()
        if self._cache.touch_mark(session_id, TOUCH_GRACE_SECONDS):
            return True
        now = datetime.now(UTC)
        async with session_scope() as session:
            result = await session.execute(
                update(PortalSession)
                .where(PortalSession.id == session_id, PortalSession.expires_at > now)
                .values(last_seen=now, expires_at=now + self._session_max_age)
            )
        return (result.rowcount or 0) > 0

    async def count(self) -> int:
        """The number of rows in the table, expired ones included, for measurement and tests."""
        async with session_scope() as session:
            return (await session.execute(select(func.count()).select_from(PortalSession))).scalar_one()

    async def purge_expired(self) -> int:
        """Sweep expired sessions; the task worker's hourly cleanup loop calls this."""
        async with session_scope() as session:
            result = await session.execute(delete(PortalSession).where(PortalSession.expires_at <= datetime.now(UTC)))
            return result.rowcount or 0

    def _report_if_due(self) -> None:
        """Log the cache counters at most once per interval, on activity and not on a timer."""
        now = time.monotonic()
        if now < self._next_report_at:
            return
        if self._next_report_at == 0.0:
            # No zero report on the very first call; the counter starts now.
            self._next_report_at = now + REPORT_INTERVAL_SECONDS
            return
        stats = self._cache.take_stats()
        sets, self._writes = self._writes, 0
        self._next_report_at = now + REPORT_INTERVAL_SECONDS
        if not (stats.hits or stats.misses or sets):
            return
        read_count = stats.hits + stats.misses
        hit_ratio = (stats.hits / read_count) if read_count else 0.0
        logger.info(
            "session cache: %d hits, %d misses (hit-ratio %.0f%%), %d sets, %d evicted at cap, %d items cached",
            stats.hits,
            stats.misses,
            100 * hit_ratio,
            sets,
            stats.evictions,
            len(self._cache),
        )


_session_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """The process-wide store, created from ``settings`` on first use."""
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
