"""De hot-cache voor server-side sessies: een begrensde FIFO met per-item TTL.

Elke request leest van hier; de database is alleen de bron bij een miss. Twee grenzen
houden hem klein en vers: maximaal ``max_entries`` items (een volle cache zakt de oudste
eruit, in volgorde van binnenkomst) en een vervaltijd per item (daarna telt hij als miss
en dwingt een database-lezing af, dus een tweede OPI-pod loopt nooit verder achter dan
die tijd). FIFO bewust geen LRU: een volgorderij is eenvoudiger en hier genoeg.
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
    """Tellers sinds de vorige :meth:`SessionCache.take_stats`."""

    hits: int
    misses: int
    evictions: int


class SessionCache:
    """Begrensde FIFO met per-item TTL, voor sessie-id's en hun data.

    ``get`` levert NOOIT een verlopen item en verplaatst niets: de volgorde is zuiver de
    volgorde van binnenkomst. Kopieert in en uit, zodat een aanroeper de bewaarde data
    niet mee muteert (een sessie-dict verandert plek in de store, niet in het cachebeeld).
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
        """De data van deze sessie, of None bij een miss of een verlopen item."""
        entry = self._entries.get(session_id)
        if entry is None:
            self._misses += 1
            return None
        if entry.expires_at <= self._clock():
            # Een verlopen item komt nooit uit de cache: de database is weer de bron.
            del self._entries[session_id]
            self._misses += 1
            return None
        self._hits += 1
        return deepcopy(entry.data)

    def set(self, session_id: str, data: dict[str, Any]) -> None:
        """Sla de data op met een verse TTL; bij een volle cache gaat de oudste eruit."""
        if session_id not in self._entries and len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1
        self._entries[session_id] = _Entry(data=deepcopy(data), expires_at=self._clock() + self._ttl)

    def delete(self, session_id: str) -> None:
        self._entries.pop(session_id, None)

    def delete_where(self, predicate: Callable[[dict[str, Any]], bool]) -> int:
        """Verwijder elke cache-entry waarvan de data aan ``predicate`` voldoet."""
        treffers = [sid for sid, entry in self._entries.items() if predicate(entry.data)]
        for sid in treffers:
            del self._entries[sid]
        return len(treffers)

    def touch_mark(self, session_id: str, grace_seconds: float) -> bool:
        """Markeer deze sessie als zojuist aangeraakt.

        Returns True als de markering VERS genoeg was (minder dan ``grace_seconds``
        geleden gezet), zodat de store de database-touch achterwege kan laten.
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
        """De tellers sinds de vorige aanroep, en zet ze op nul (voor de minuutlog)."""
        stats = CacheStats(hits=self._hits, misses=self._misses, evictions=self._evictions)
        self._hits = self._misses = self._evictions = 0
        return stats

    def __len__(self) -> int:
        return len(self._entries)
