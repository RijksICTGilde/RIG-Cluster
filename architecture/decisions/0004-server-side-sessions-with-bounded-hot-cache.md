# 0004 - Server-side sessions with a bounded hot cache

**Status**: Accepted
**Date**: 2026-10-27

## Context

On 2026-10-27 OTP registration broke on the sandbox: the session cookie carried the
id_token plus the fingerprint of the pending registration, and crossed the ~4 KB a
browser silently drops. PR 370 fixed that day by moving the pending registration
server-side (table `otp_pending_registrations`), but the structural point remained:
every server-side addition to the session made the cookie heavier, and the next feature
writing one more key would hit the same ceiling. The session also carried values that
get safer semantics when they live server-side (`login_at` for the absolute lifetime,
and future ones like a step-up stamp).

AMT/TAD had solved this earlier with its own store.

## Decision

Session content moves server-side (RC-390):

- Table `sessions` (migration 011) holds the content as jsonb with `created_at`,
  `expires_at` and `last_seen`. The cookie carries only a signed session id
  (itsdangerous, as before): small and constant in size, regardless of what the
  session contains.
- `SessionStore` (`opi/services/session_store.py`) is read-through on reads (cache,
  then table, then fill the cache) and write-through on writes (`set` fills the cache
  and upserts the row, so a next reader sees a mutation immediately, even mid-request).
- The hot cache (`opi/core/session_cache.py`) is bounded: at most
  `SESSION_CACHE_MAX_ENTRIES` entries (default 5000) plus a per-item TTL
  (`SESSION_CACHE_TTL_SECONDS`, default 60 seconds). After the TTL the database is the
  source again, so a second OPI pod never lags more than that minute. (The eviction
  order recorded here as FIFO was amended to LRU by
  [0005](0005-hot-cache-evicts-least-recently-used.md).)
- `ServerSideSessionMiddleware` (`opi/middleware/server_side_session.py`) replaces
  Starlette's SessionMiddleware as a drop-in: `request.session` stays a dict, cookie
  attributes (`httponly`, `same_site=lax`, `Secure` outside DEBUG, `Max-Age`) are
  unchanged. Static paths and probes skip the session entirely (same exclusion list as
  the authorization middleware), so they cause no store or cache traffic.
- Logout and revocation delete the row (next to the existing `session_revocations`
  post), turning revocation into a hard refusal on the next request. Logging in rotates
  the session id (`regenerate` in `auth_callback`), closing session fixation via cookie
  tossing. Expired rows are purged by the task worker's hourly loop.

The working details live in `features/server-side-sessies.md`; this record captures
the choice.

## Consequences

**Positive:**

- The cookie size is decoupled from session content: no feature can push it over the
  browser limit again.
- Revocation and logout are hard guarantees server-side, not best-effort cookie expiry.
- Session fixation on login is closed structurally (new id at the privilege hinge).
- The read path is cache-served per request; the database sees a miss per new session
  per pod, not per request. A once-per-minute INFO log reports the cache hit ratio.

**Negative / costs:**

- Every session mutation writes to rig-db; a failed write logs an error and leaves the
  reader to its last known state until the TTL or the next request.
- The in-memory cache with TTL lets a second replica lag up to the TTL on another pod's
  mutations. Production runs one replica today, so this is empty; with a second replica
  the cache agreement becomes its own decision.
- On rollout, everyone logged in at that moment is logged out once: a cookie in the old
  shape reads as an unknown id, meaning an empty session. No dual-read is built; this
  is noted for the release notes.
- An extra table plus cache to operate and reason about, bounded by the hourly purge.
