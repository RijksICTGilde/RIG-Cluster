"""The hot cache for server-side sessions: a bounded LRU with a per-item TTL.

Every request reads from here; the database is the source only on a miss. Two bounds keep it
small and fresh: at most ``max_entries`` items (a full cache drops the least recently touched
one) and an expiry per item (after which it counts as a miss and forces a database read, so a
second OPI pod never lags further behind than that time).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from opi.core.config import settings


@dataclass
class _Entry:
    data: dict[str, Any]
    expires_at: float
    touched_at: float = field(default=0.0)


@dataclass(frozen=True)
class CacheStats:
    """Counters since the previous :meth:`SessionCache.take_stats`."""

    hits: int
    misses: int
    evictions: int


class SessionCache:
    """Bounded LRU with a per-item TTL, for session ids and their data.

    ``get`` NEVER hands out an expired item and moves the hit to the end of the queue (most
    recent); ``set`` does the same. Eviction therefore loses whatever went untouched longest.
    Copies in and out, so a caller does not mutate the stored data along with its own (a
    session dict changes in the store, not in the cached picture).

    Eviction goes by recency alone, not by expiry: an expired item keeps its place until the
    first read after it, even when a valid but cold item gives way before it.
    """

    def __init__(
        self,
        max_entries: int | None = None,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = settings.SESSION_CACHE_MAX_ENTRIES if max_entries is None else max_entries
        self._ttl = settings.SESSION_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, session_id: str) -> dict[str, Any] | None:
        """The data of this session, or None on a miss or an expired item."""
        entry = self._entries.get(session_id)
        if entry is None:
            self._misses += 1
            return None
        if entry.expires_at <= self._clock():
            # An expired item never comes out of the cache: the database is the source again.
            del self._entries[session_id]
            self._misses += 1
            return None
        self._entries.move_to_end(session_id)
        self._hits += 1
        return deepcopy(entry.data)

    def set(self, session_id: str, data: dict[str, Any]) -> None:
        """Store the data with a fresh TTL and a fresh position; a full cache drops the least
        recently touched entry.
        """
        existing = session_id in self._entries
        if not existing and len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1
        self._entries[session_id] = _Entry(data=deepcopy(data), expires_at=self._clock() + self._ttl)
        self._entries.move_to_end(session_id)

    def delete(self, session_id: str) -> None:
        self._entries.pop(session_id, None)

    def delete_where(self, predicate: Callable[[dict[str, Any]], bool]) -> int:
        """Remove every cache entry whose data satisfies ``predicate``."""
        matches = [sid for sid, entry in self._entries.items() if predicate(entry.data)]
        for sid in matches:
            del self._entries[sid]
        return len(matches)

    def touch_mark(self, session_id: str, grace_seconds: float) -> bool:
        """Mark this session as touched just now.

        Returns True when the mark was FRESH enough (set less than ``grace_seconds`` ago), so
        the store can leave the database touch out.
        """
        entry = self._entries.get(session_id)
        if entry is None or entry.expires_at <= self._clock():
            return False
        now = self._clock()
        if now - entry.touched_at < grace_seconds:
            return True
        entry.touched_at = now
        return False

    def take_stats(self) -> CacheStats:
        """The counters since the previous call, reset to zero (for the per-minute log)."""
        stats = CacheStats(hits=self._hits, misses=self._misses, evictions=self._evictions)
        self._hits = self._misses = self._evictions = 0
        return stats

    def __len__(self) -> int:
        return len(self._entries)
