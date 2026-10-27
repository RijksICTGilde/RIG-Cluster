"""De grenzen van de sessie-hot-cache (RC-390): FIFO-verdringing, TTL en kopie-semantiek."""

from __future__ import annotations

from opi.core.config import settings
from opi.core.session_cache import SessionCache


def _clock_fixture_bits() -> tuple[list[float], object]:
    """Een handmatige klok: de test schuift zelf de tijd op."""
    now = [1000.0]
    return now, (lambda: now[0])


def test_a_miss_returns_none_and_counts() -> None:
    cache = SessionCache(max_entries=10, ttl_seconds=60)

    assert cache.get("onbekend") is None

    assert cache.take_stats().misses == 1


def test_set_then_get_is_a_hit() -> None:
    cache = SessionCache(max_entries=10, ttl_seconds=60)
    cache.set("s1", {"user": {"email": "a@b.nl"}})

    assert cache.get("s1") == {"user": {"email": "a@b.nl"}}

    stats = cache.take_stats()
    assert stats.hits == 1
    assert stats.misses == 0


def test_an_expired_item_never_comes_from_the_cache() -> None:
    now, clock = _clock_fixture_bits()
    cache = SessionCache(max_entries=10, ttl_seconds=60, clock=clock)  # type: ignore[arg-type]
    cache.set("s1", {"v": 1})

    now[0] += 61

    assert cache.get("s1") is None
    assert len(cache) == 0, "een verlopen item laat zijn plek ook echt vrij"
    assert cache.take_stats().misses == 1


def test_a_full_cache_evicts_the_oldest() -> None:
    """FIFO: de volgorde van binnenkomst bepaalt wie weg moet, niet wie recent gelezen is."""
    cache = SessionCache(max_entries=3, ttl_seconds=60)
    for sid in ("s1", "s2", "s3"):
        cache.set(sid, {"v": sid})
    # s1 recent gelezen mag de FIFO-volgorde niet redden.
    assert cache.get("s1") is not None

    cache.set("s4", {"v": "s4"})

    assert cache.get("s1") is None
    assert cache.get("s2") is not None
    assert cache.take_stats().evictions == 1


def test_a_rewritten_id_keeps_its_original_position() -> None:
    """Puur FIFO: een set overschrijft de data maar ververst de positie niet."""
    cache = SessionCache(max_entries=2, ttl_seconds=60)
    cache.set("s1", {"v": 1})
    cache.set("s2", {"v": 2})
    cache.set("s1", {"v": 9})
    assert cache.get("s1") == {"v": 9}, "overschreven is overschreven"

    cache.set("s3", {"v": 3})

    assert cache.get("s1") is None, "s1 was eerst binnen, ook al is hij later overschreven"
    assert cache.get("s2") is not None
    assert cache.get("s3") is not None


def test_returned_data_is_a_copy() -> None:
    """Een aanroeper die zijn teruggegeven dict muteert mag de cache niet mee veranderen."""
    cache = SessionCache(max_entries=10, ttl_seconds=60)
    stored = {"user": {"email": "a@b.nl"}}
    cache.set("s1", stored)
    stored["user"]["email"] = "gemuteerd@b.nl"

    read_back = cache.get("s1")
    assert read_back == {"user": {"email": "a@b.nl"}}

    assert read_back is not None
    read_back["user"]["email"] = "ook-gemuteerd@b.nl"
    assert cache.get("s1") == {"user": {"email": "a@b.nl"}}


def test_delete_removes_the_item() -> None:
    cache = SessionCache(max_entries=10, ttl_seconds=60)
    cache.set("s1", {"v": 1})
    cache.delete("s1")

    assert cache.get("s1") is None
    assert cache.take_stats().misses == 1


def test_delete_where_removes_by_predicate() -> None:
    cache = SessionCache(max_entries=10, ttl_seconds=60)
    cache.set("s1", {"user": {"email": "a@b.nl"}})
    cache.set("s2", {"user": {"email": "c@d.nl"}})

    assert cache.delete_where(lambda data: (data.get("user") or {}).get("email") == "a@b.nl") == 1
    assert cache.get("s1") is None
    assert cache.get("s2") is not None


def test_touch_mark_throttles_within_grace() -> None:
    cache = SessionCache(max_entries=10, ttl_seconds=60)
    cache.set("s1", {"v": 1})

    assert cache.touch_mark("s1", grace_seconds=60) is False, "eerste touch: de store moet naar de database"
    assert cache.touch_mark("s1", grace_seconds=60) is True, "binnen de grace is de markering genoeg"


def test_take_stats_resets_the_counters() -> None:
    cache = SessionCache(max_entries=10, ttl_seconds=60)
    cache.get("x")
    stats = cache.take_stats()
    assert stats.misses == 1

    assert cache.take_stats().misses == 0


def test_the_default_ttl_comes_from_the_settings() -> None:
    """Zonder argument geldt de TTL uit de config: daarna is de database weer de bron."""
    now, clock = _clock_fixture_bits()
    cache = SessionCache(clock=clock)  # type: ignore[arg-type]
    cache.set("s1", {"v": 1})

    now[0] += settings.SESSION_CACHE_TTL_SECONDS - 1
    assert cache.get("s1") is not None

    now[0] += 2
    assert cache.get("s1") is None


def test_the_default_cap_comes_from_the_settings() -> None:
    """Zonder argument geldt het maximum uit de config: de oudste zakt eruit."""
    cache = SessionCache()
    for i in range(settings.SESSION_CACHE_MAX_ENTRIES + 1):
        cache.set(f"s{i}", {"v": i})

    assert len(cache) == settings.SESSION_CACHE_MAX_ENTRIES
    assert cache.get("s0") is None
    assert cache.take_stats().evictions == 1
