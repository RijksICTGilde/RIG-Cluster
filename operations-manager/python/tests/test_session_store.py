"""De SessionStore (RC-390) tegen een echte Postgres (``orm_db``).

Getoetst worden de twee kanten van read-through (miss naar de tabel, hit uit de cache),
de directe zichtbaarheid van write-through, de grenzen van ``touch`` (verlengt, maar
reanimeert niet), en de opruiming.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opi.core.db import session_scope
from opi.core.session_cache import SessionCache
from opi.services import persistence  # noqa: F401  -- registreert de ORM-modellen op Base.metadata
from opi.services.persistence.sessions import PortalSession
from opi.services.session_store import SessionStore
from sqlalchemy import select

if TYPE_CHECKING:
    import pytest
    from opi.core.session_cache import CacheStats

ONE_HOUR = timedelta(hours=1)


def _store(**kwargs) -> tuple[SessionStore, SessionCache]:
    cache = kwargs.pop("cache", SessionCache(max_entries=100, ttl_seconds=60))
    return SessionStore(cache=cache, session_max_age=ONE_HOUR), cache


async def _row(session_id: str) -> PortalSession | None:
    async with session_scope() as db:
        return (await db.execute(select(PortalSession).where(PortalSession.id == session_id))).scalar_one_or_none()


async def _lay_expired_row(session_id: str, data: dict) -> None:
    """Een rij waarvan het verloop al voorbij is, alsof de purge er nog niet aan toe kwam."""
    verleden = datetime.now(UTC) - timedelta(minutes=5)
    async with session_scope() as db:
        db.add(PortalSession(id=session_id, data=data, created_at=verleden, expires_at=verleden, last_seen=verleden))


async def test_get_without_row_is_none(orm_db: None) -> None:
    store, cache = _store()

    assert await store.get("onbekend") is None
    assert cache.take_stats().misses == 1


async def test_get_does_not_return_an_expired_row(orm_db: None) -> None:
    """Een verlopen rij die de purge nog niet ruimde komt niet terug, ook niet via de cache."""
    store, cache = _store()
    await _lay_expired_row("s-oud", {"v": 1})

    assert await store.get("s-oud") is None
    assert cache.get("s-oud") is None, "een verlopen rij mag de cache niet eens vullen"


async def test_set_is_immediately_readable_and_write_through(orm_db: None) -> None:
    """Zowel de cache als de tabel dragen de mutatie: een andere lezer (lege cache) ziet hem."""
    store, _ = _store()

    await store.set("s1", {"user": {"email": "a@b.nl"}})

    assert await store.get("s1") == {"user": {"email": "a@b.nl"}}
    rij = await _row("s1")
    assert rij is not None
    assert rij.data == {"user": {"email": "a@b.nl"}}

    koude_store, koude_cache = _store()
    assert await koude_store.get("s1") == {"user": {"email": "a@b.nl"}}
    assert koude_cache.take_stats().misses == 1, "de eerste lezing van een koude cache gaat naar de tabel"

    assert await koude_store.get("s1") is not None
    stats: CacheStats = koude_cache.take_stats()
    assert stats.hits == 1
    assert stats.misses == 0, "daarna bedient de cache"


async def test_set_overwrites_the_row(orm_db: None) -> None:
    store, _ = _store()
    await store.set("s1", {"v": 1})

    await store.set("s1", {"v": 2})

    assert await store.get("s1") == {"v": 2}
    assert (await _row("s1")).data == {"v": 2}  # type: ignore[union-attr]


async def test_delete_removes_row_and_cache(orm_db: None) -> None:
    store, _ = _store()
    await store.set("s1", {"v": 1})

    await store.delete("s1")

    assert await store.get("s1") is None
    assert await _row("s1") is None


async def test_touch_extends_expiry(orm_db: None) -> None:
    store, _ = _store()
    await store.set("s1", {"v": 1})
    # Zet het verloop vlak achter nu, alsof de sessie bijna zou verlopen.
    bijna = datetime.now(UTC) + timedelta(minutes=1)
    async with session_scope() as db:
        rij = (await db.execute(select(PortalSession).where(PortalSession.id == "s1"))).scalar_one()
        rij.expires_at = bijna

    assert await store.touch("s1") is True

    rij = await _row("s1")
    assert rij is not None
    assert rij.expires_at > datetime.now(UTC) + timedelta(minutes=30)


async def test_touch_does_not_revive_an_expired_row(orm_db: None) -> None:
    """Een verlopen sessie blijft dood: touch kan geen oneindige verlenging afdwingen."""
    store, _ = _store()
    await _lay_expired_row("s-oud", {"v": 1})

    assert await store.touch("s-oud") is False

    rij = await _row("s-oud")
    assert rij is not None
    assert rij.expires_at < datetime.now(UTC)


async def test_touch_throttles_the_database_within_grace(orm_db: None) -> None:
    store, cache = _store()
    await store.set("s1", {"v": 1})
    # De cache is al warm door de set: de eerste touch zet de markering en schrijft.
    cache.touch_mark("s1", 3600)

    assert await store.touch("s1") is True, "binnen de grace is de markering genoeg - geen database-schrijving"


async def test_purge_expired_cleans_up_and_keeps_fresh(orm_db: None) -> None:
    store, _ = _store()
    await store.set("vers", {"v": 1})
    await _lay_expired_row("oud", {"v": 1})

    assert await store.purge_expired() == 1
    assert await _row("oud") is None
    assert await _row("vers") is not None


async def test_count_counts_the_rows(orm_db: None) -> None:
    store, _ = _store()
    assert await store.count() == 0

    await store.set("s1", {"v": 1})
    await store.set("s2", {"v": 2})

    assert await store.count() == 2


async def test_delete_by_email_removes_all_sessions_of_the_address(orm_db: None) -> None:
    """Intrekken betekent bij server-side sessies: de rij weg, en op de volgende request een
    harde weigering - ook voor de warme cache."""
    store, cache = _store()
    await store.set("s1", {"user": {"email": "A@b.nl"}})
    await store.set("s2", {"user": {"email": "c@d.nl"}})
    await store.get("s1")

    assert await store.delete_by_email("a@b.nl") == 1

    assert await _row("s1") is None
    assert await _row("s2") is not None
    assert cache.get("s1") is None, "ook de cache laat hem vallen, anders liep hij de TTL nog warm"
    assert cache.get("s2") is not None


def test_the_report_logs_hits_misses_and_sets_per_interval(caplog: pytest.LogCaptureFixture) -> None:
    """De minuutmeting van de sandbox: hit-ratio en schrijfacties als INFO."""
    import logging

    store, cache = _store()
    store._next_report_at = time.monotonic() - 1  # rapport mag nu al
    with caplog.at_level(logging.INFO, logger="opi.services.session_store"):
        cache.get("x")
        store._report_if_due()

    assert "session cache:" in caplog.text
    assert "hit-ratio" in caplog.text
