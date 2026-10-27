# 0005 - Hot cache evicts least recently used, not first in

**Status**: Accepted
**Date**: 2026-10-27

## Context

[ADR 0004](0004-server-side-sessions-with-bounded-hot-cache.md) introduced the
bounded hot cache for server-side sessions with FIFO
eviction: the entry that arrived first left first, even if it had just been read.
FIFO was recorded as a deliberate choice there ("simpler and sufficient"). In
practice arrival order is wrong for a cache: an active session hit on every
request could be evicted while an idle one survived, purely because it entered
earlier. Every read having a say in what stays is exactly the property the cache
exists for (RC-391).

## Decision

The hot cache (`opi/core/session_cache.py`) evicts by recency instead of arrival
order. A `get` hit and a `set` of an existing key both move the entry to the most
recent end, so a full cache drops the least recently touched entry. This amends
the eviction-order bullet of ADR 0004; everything else in ADR 0004 stands,
including the per-item TTL (expired means miss, the database is the source again)
and the `SESSION_CACHE_MAX_ENTRIES` / `SESSION_CACHE_TTL_SECONDS` bounds.

Eviction considers recency only, not expiry: an expired entry keeps its slot until
the first read after expiry, even if a valid but cold entry yields for it. That is
deliberate, so a full `set` does no scan.

## Consequences

**Positive:**

- Active sessions keep their cache entry: every request counts as a touch, so a
  flood of new sessions cannot push out sessions that are actually in use.
- The cache hit ratio better reflects real usage, since hot entries survive.

**Negative / costs:**

- Two extra `move_to_end` calls on the read and write paths; still O(1), no
  measurable cost.
- Because eviction ignores expiry, a cache full of expired entries still evicts a
  live cold entry on `set`; the expired slots are only reclaimed on their first
  read after expiry. Accepted in exchange for keeping `set` a constant-time pop.
