"""Server-side sessies (RC-390): de tabel ``sessions`` met de hot-cache ervoor.

Leesweg: eerst de cache, bij een miss de tabel (die vult de cache). Schrijfweg is
write-through: ``set`` vult de cache én upsert de rij, dus een mutatie is voor een
volgende lezer direct zichtbaar.

Het verloop loopt op twee plaatsen: ``expires_at`` hier (het schuivende
inactiviteitsvenster; ``purge_expired`` ruimt op vanuit de uurlijkse lus van de
taakwerker) en ``user.login_at`` in de data (de absolute grens, gehandhaafd door
``opi/core/session_guard.py``).

De hit-ratio en het aantal sets worden hooguit eens per minuut als INFO gelogd, op
activiteit: een store zonder verkeer heeft niets te melden.
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

#: Een touch schrijft de database hooguit eens per zoveel seconden per sessie bij. Binnen
#: die ruimte is de cachemarkering genoeg; de rij zelf mag er even achter lopen.
TOUCH_GRACE_SECONDS = 60.0

#: Hoe vaak de cachetellers hooguit gelogd worden.
REPORT_INTERVAL_SECONDS = 60.0


class SessionStore:
    """De rij in ``sessions`` achter een sessie-id, met de hot read-through cache ervoor."""

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
        """De data van deze sessie, of None als hij onbekend of verlopen is."""
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
        """Write-through: cache vullen én de rij upserten met een vers verloop."""
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
        """Weg bij beiden: de cache en de rij."""
        self._report_if_due()
        self._cache.delete(session_id)
        async with session_scope() as session:
            await session.execute(delete(PortalSession).where(PortalSession.id == session_id))

    async def delete_by_email(self, email: str) -> int:
        """Verwijder elke sessie van dit adres; het intrekken is dan een harde weigering."""
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
        """Verleng het verloop van deze sessie; nooit boven het verloop van een dode rij uit.

        Returns True als er een rij verlengd is. Een verlopen of afwezige rij wordt niet
        gereanimeerd, dus touch kan een sessie niet oneindig in leven houden. Binnen
        :data:`TOUCH_GRACE_SECONDS` na de vorige touch is de cachemarkering genoeg en
        blijft de database uit de weg.
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
        """Het aantal rijen in de tabel, vervallen inbegrepen (voor meting en tests)."""
        async with session_scope() as session:
            return (await session.execute(select(func.count()).select_from(PortalSession))).scalar_one()

    async def purge_expired(self) -> int:
        """Ruim verlopen sessies op; de uurlijkse cleanup-lus van de taakwerker roept dit."""
        async with session_scope() as session:
            result = await session.execute(delete(PortalSession).where(PortalSession.expires_at <= datetime.now(UTC)))
            return result.rowcount or 0

    def _report_if_due(self) -> None:
        """Log de cachetellers hooguit eens per interval, op activiteit in plaats van een timer."""
        now = time.monotonic()
        if now < self._next_report_at:
            return
        if self._next_report_at == 0.0:
            # Niet bij de eerste aanroep al een nul-rapport loggen; de teller start nu.
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
    """De proceswijde store. Aangemaakt met de waarden uit ``settings`` bij eerste gebruik."""
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
