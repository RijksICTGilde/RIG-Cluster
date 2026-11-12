"""The token-bucket limiter and the limits OPI lays down with it.

Two layers that do not replace each other: a strict failure counter on the key paths, so
that guessing an X-API-Key stalls before the comparison runs, and a coarse upper limit per
client on the API as a whole, so that a flood does not take OPI down. Both refuse at once
with a 429 and a ``Retry-After``: a delay would hold up OPI itself, because a request kept
open costs a slot in the event loop.

This is the innermost layer. An ingress can lay a coarse, volumetric limit per IP before a
request reaches Python, but it does not know our authentication and so cannot limit per key
or per project. The counters sit in this process's memory, which holds as long as OPI is
one process per cluster.
"""

import hashlib
import logging
import math
import threading
import time
from collections import defaultdict
from typing import NoReturn

from fastapi import HTTPException
from starlette.requests import Request  # noqa: TC002 -- FastAPI needs Request at runtime

from opi.core.config import settings
from opi.core.logging_redact import fingerprint_for_log
from opi.utils.client_ip import client_ip

logger = logging.getLogger(__name__)


def _ip_of(client_id: str) -> str:
    """What the second bucket counts on: the first field of the client id."""
    return client_id.split("|")[0] if "|" in client_id else client_id


def _wait_for_one(available: float, rate: float) -> float:
    """How long until a bucket with ``available`` tokens has one."""
    if available >= 1:
        return 0.0
    return (1 - available) / rate if rate > 0 else math.inf


def _refill_seconds(burst: int, rate: float) -> float:
    """How long an empty bucket takes to be back at its full burst."""
    return burst / rate if rate > 0 else math.inf


class IPRateLimiter:
    """Two-layer limiter: per client id and per IP, with emergency cleanup."""

    MAX_TRACKED_CLIENTS = 10000
    CLEANUP_THRESHOLD = 8000
    EMERGENCY_PURGE_MIN_AGE_SECONDS = 60
    # Without this minimum between two emergency cleanups the scan runs per request.
    EMERGENCY_CLEANUP_INTERVAL_SECONDS = 10.0
    MAX_NEW_CLIENTS_PER_MINUTE = 100

    def __init__(
        self,
        requests_per_minute: float = 30,
        burst: int = 10,
        cleanup_interval: float = 60.0,
        per_ip_requests_per_minute: float | None = None,
        max_new_clients_per_minute: int | None = MAX_NEW_CLIENTS_PER_MINUTE,
    ):
        # float, not int: a limit per HOUR is less than one request per minute (the invite
        # routes), and the sum below already divides by 60.0.
        self.rate = requests_per_minute / 60.0  # requests per second
        self.burst = burst
        self._tokens: dict[str, float] = defaultdict(lambda: float(burst))
        self._last_update: dict[str, float] = defaultdict(time.monotonic)
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.monotonic()
        # 0.0 is "long ago": the first emergency cleanup may run at once.
        self._last_emergency_cleanup = 0.0
        # Thread safety for concurrent async requests.
        self._lock = threading.Lock()

        # Second bucket per IP, which swapping fingerprints cannot get around. Three times
        # the normal rate by default, to allow users with several sessions.
        self._per_ip_rate = (per_ip_requests_per_minute or (requests_per_minute * 3)) / 60.0
        self._per_ip_burst = burst * 3
        self._ip_tokens: dict[str, float] = defaultdict(lambda: float(self._per_ip_burst))
        self._ip_last_update: dict[str, float] = defaultdict(time.monotonic)

        # A bucket may not be cleaned up before it would have refilled itself anyway,
        # otherwise a per-hour limit does not hold for an hour.
        self._min_retention_seconds = max(
            _refill_seconds(burst, self.rate),
            _refill_seconds(self._per_ip_burst, self._per_ip_rate),
        )

        # Timestamps of new clients, to detect identifier flooding.
        self._new_client_timestamps: list[float] = []
        # None switches that counter off. On a limit that applies to EVERY request it is a
        # refusal of every new visitor as soon as someone invents client ids.
        self._max_new_clients_per_minute = max_new_clients_per_minute

    @staticmethod
    def get_client_fingerprint(request: Request) -> str:
        """Hash of User-Agent and headers: a second factor beside the IP in the client id."""
        user_agent = request.headers.get("user-agent", "")
        accept_language = request.headers.get("accept-language", "")
        accept_encoding = request.headers.get("accept-encoding", "")

        fingerprint_data = f"{user_agent}|{accept_language}|{accept_encoding}"
        return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]

    @classmethod
    def get_client_identifier(cls, request: Request) -> str:
        """Client id: IP | fingerprint | session id.

        The IP comes from ``client_ip``, the one place that determines it: a caller who
        invents their ``X-Forwarded-For`` may not be able to pick their own bucket.
        """
        ip = client_ip(request)
        fingerprint = cls.get_client_fingerprint(request)

        session_id = ""
        # AssertionError: that is how request.session fails outside SessionMiddleware.
        try:
            session = request.session
        except AssertionError, AttributeError:
            session = None
        if session:
            session_id = session.get("_session_id", "")[:32]

        # Combined into ip|fingerprint|session, the session empty without authentication.
        return f"{ip}|{fingerprint}|{session_id}"

    def _check_new_client_rate_unlocked(self) -> bool:
        """Whether too many new client ids were created, under the lock."""
        if self._max_new_clients_per_minute is None:
            return True

        now = time.monotonic()
        # Drop timestamps older than a minute.
        self._new_client_timestamps = [ts for ts in self._new_client_timestamps if now - ts < 60]

        if len(self._new_client_timestamps) >= self._max_new_clients_per_minute:
            logger.warning(
                f"New client rate limit exceeded: {len(self._new_client_timestamps)} new clients in last minute"
            )
            return False

        return True

    def _check_per_ip_limit_unlocked(self, ip: str) -> bool:
        """The second bucket, per IP, under the lock."""
        now = time.monotonic()
        elapsed = now - self._ip_last_update[ip]
        self._ip_last_update[ip] = now

        # Add tokens for the time elapsed.
        self._ip_tokens[ip] = min(self._per_ip_burst, self._ip_tokens[ip] + elapsed * self._per_ip_rate)

        if self._ip_tokens[ip] >= 1:
            self._ip_tokens[ip] -= 1
            return True
        return False

    def is_allowed(self, client_id: str) -> bool:
        """Check both buckets and clean up old entries. Thread-safe."""
        with self._lock:
            now = time.monotonic()

            # Take the IP out of the client id for the second bucket.
            ip = _ip_of(client_id)

            # Only a new client counts towards the flooding check.
            is_new_client = client_id not in self._last_update
            if is_new_client:
                if not self._check_new_client_rate_unlocked():
                    return False
                # A new client has its full burst, so only the per-IP bucket can still
                # refuse it. READ that one first: a refusal that leaves a bucket behind
                # lets the attacker decide what a refusal costs.
                if self._ip_available_unlocked(ip, now) < 1:
                    logger.debug(f"Per-IP rate limit exceeded for new client on {ip}")
                    return False
                self._new_client_timestamps.append(now)
                self._evict_overflow_unlocked()

            elapsed = now - self._last_update[client_id]
            self._last_update[client_id] = now

            # Add tokens for the time elapsed.
            self._tokens[client_id] = min(self.burst, self._tokens[client_id] + elapsed * self.rate)

            # Periodic cleanup, on time.
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_old_entries_unlocked()
                self._last_cleanup = now
            # Emergency cleanup when memory threatens to fill up, at most once per
            # interval: per request it is two O(n) scans under the lock, and then a flood
            # decides what every request of every client costs.
            elif (
                self._tracked_count_unlocked() > self.CLEANUP_THRESHOLD
                and now - self._last_emergency_cleanup > self.EMERGENCY_CLEANUP_INTERVAL_SECONDS
            ):
                self._last_emergency_cleanup = now
                self._cleanup_old_entries_unlocked(max_age_seconds=300)
                # Still full after the cleanup: a more aggressive purge.
                if self._tracked_count_unlocked() > self.MAX_TRACKED_CLIENTS:
                    self._gradual_purge_unlocked()

            # First check: the bucket per client id.
            if self._tokens[client_id] < 1:
                return False

            # Second check: the bucket per IP, which a fingerprint swap cannot get around.
            if not self._check_per_ip_limit_unlocked(ip):
                logger.debug(f"Per-IP rate limit exceeded for {ip}")
                return False

            self._tokens[client_id] -= 1
            return True

    def _tracked_count_unlocked(self) -> int:
        """How many buckets the cleanup has to watch; both kinds count.

        Looking at ``_tokens`` alone lets the per-IP buckets grow without limit, and every
        request creates one of those.
        """
        return max(len(self._tokens), len(self._ip_tokens))

    def has_tokens(self, client_id: str) -> bool:
        """Whether this client would be allowed a request now, without taking a token.

        For a limit that counts down on failure only: the check may not take a successful
        authentication out of the budget of the next failed attempt.
        """
        with self._lock:
            now = time.monotonic()
            ip = _ip_of(client_id)
            return self._available_unlocked(client_id, now) >= 1 and self._ip_available_unlocked(ip, now) >= 1

    def seconds_until_token(self, client_id: str) -> float:
        """How long until this client has a token again, in both buckets."""
        with self._lock:
            now = time.monotonic()
            ip = _ip_of(client_id)
            return max(
                _wait_for_one(self._available_unlocked(client_id, now), self.rate),
                _wait_for_one(self._ip_available_unlocked(ip, now), self._per_ip_rate),
            )

    def _available_unlocked(self, client_id: str, now: float) -> float:
        """This client's token balance, without creating or counting down the bucket.

        An unknown client has its full burst: only counting down may create a bucket,
        otherwise reading is a way to make the memory grow.
        """
        if client_id not in self._last_update:
            return float(self.burst)
        return min(self.burst, self._tokens[client_id] + (now - self._last_update[client_id]) * self.rate)

    def _ip_available_unlocked(self, ip: str, now: float) -> float:
        """The token balance of the second bucket, which counts per IP. Reads, does not count down."""
        if ip not in self._ip_last_update:
            return float(self._per_ip_burst)
        return min(self._per_ip_burst, self._ip_tokens[ip] + (now - self._ip_last_update[ip]) * self._per_ip_rate)

    def _evict_overflow_unlocked(self) -> None:
        """Keep both bucket collections under ``MAX_TRACKED_CLIENTS``, in O(1) per entry.

        Evicts in creation order (a dict keeps that), so the upper bound is hard even when
        nothing is old enough to clean up. Eviction gives a client its full burst back; the
        per-IP bucket stays the backstop, and the address in it cannot be picked
        (``client_ip`` counts hops from the right).
        """
        while len(self._last_update) >= self.MAX_TRACKED_CLIENTS:
            oldest = next(iter(self._last_update))
            del self._last_update[oldest]
            self._tokens.pop(oldest, None)
        while len(self._ip_last_update) >= self.MAX_TRACKED_CLIENTS:
            oldest_ip = next(iter(self._ip_last_update))
            del self._ip_last_update[oldest_ip]
            self._ip_tokens.pop(oldest_ip, None)

    def cleanup_old_entries(self, max_age_seconds: int = 600) -> None:
        """Remove old entries, with the refill window as the lower bound."""
        with self._lock:
            self._cleanup_old_entries_unlocked(max_age_seconds)

    def _cleanup_old_entries_unlocked(self, max_age_seconds: int = 600) -> None:
        """Clean up under the lock, with the refill window as the lower bound."""
        now = time.monotonic()
        max_age = max(max_age_seconds, self._min_retention_seconds)
        to_remove = [ip for ip, last in self._last_update.items() if now - last > max_age]
        for ip in to_remove:
            del self._tokens[ip]
            del self._last_update[ip]

        # Cleanup for the per-IP buckets as well.
        ip_to_remove = [ip for ip, last in self._ip_last_update.items() if now - last > max_age]
        for ip in ip_to_remove:
            del self._ip_tokens[ip]
            del self._ip_last_update[ip]

        if to_remove or ip_to_remove:
            logger.debug(
                f"Rate limiter cleanup: removed {len(to_remove)} client entries, {len(ip_to_remove)} IP entries"
            )

    def _gradual_purge_unlocked(self) -> None:
        """Emergency cleanup: removes only entries older than the minimum age."""
        if not self._last_update and not self._ip_last_update:
            return

        now = time.monotonic()
        min_age = self.EMERGENCY_PURGE_MIN_AGE_SECONDS

        # Remove only entries older than the minimum age.
        to_remove = [client_id for client_id, last_update in self._last_update.items() if now - last_update > min_age]

        for client_id in to_remove:
            del self._tokens[client_id]
            del self._last_update[client_id]

        ip_to_remove = [ip for ip, last_update in self._ip_last_update.items() if now - last_update > min_age]
        for ip in ip_to_remove:
            del self._ip_tokens[ip]
            del self._ip_last_update[ip]

        if to_remove or ip_to_remove:
            logger.warning(
                f"Rate limiter gradual purge: removed {len(to_remove)} stale entries and "
                f"{len(ip_to_remove)} stale IP entries (>{min_age}s old)"
            )

        # Still full: real trouble. Fresh entries stay, they are probably legitimate.
        if self._tracked_count_unlocked() > self.MAX_TRACKED_CLIENTS:
            logger.error(
                f"Rate limiter still over limit ({self._tracked_count_unlocked()} entries) after gradual purge. "
                f"Possible DoS attack or configuration issue."
            )


#: The failure counter per client. The bucket key is the client id as ``IPRateLimiter``
#: makes it, so the IP comes first and the limiter's second bucket is the backstop against a
#: guesser who swaps their user-agent. Because that bucket counts on the IP alone, this
#: counter only refuses after a failed comparison: see ``refuse_when_key_failures_exhausted``.
AUTH_FAILURE_PER_CLIENT = IPRateLimiter(
    requests_per_minute=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
    burst=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
)

#: The failure counter per presented key, for a guesser who does change client id but keeps
#: trying the same key. Per key, so another party's failed attempts do not weigh on a valid
#: key: that one has a different fingerprint, and only failure counts down. Two separate
#: limiters therefore, as ``opi/api/invite_routes.py`` already does it; one bucket that puts
#: both in a string silently shifts what the second bucket counts on.
AUTH_FAILURE_PER_KEY = IPRateLimiter(
    requests_per_minute=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
    burst=settings.AUTH_FAILURE_LIMIT_PER_MINUTE,
)

#: The coarse upper limit on the API as a whole, against a flood of requests that are well
#: formed. Generous: meant for the peak, not for normal use. Without the new-client limit,
#: which would refuse every new visitor here as soon as someone swaps their user-agent.
#: Instead the limiter keeps the cost flat itself: a refused request creates no bucket,
#: ``MAX_TRACKED_CLIENTS`` is a hard upper bound with eviction, and the cleanup scan is not
#: in the hot path.
API_FLOOD_LIMITER = IPRateLimiter(
    requests_per_minute=settings.API_RATE_LIMIT_PER_MINUTE,
    burst=settings.API_RATE_LIMIT_BURST,
    max_new_clients_per_minute=None,
)


def retry_after_seconds(limiter: IPRateLimiter, client_id: str) -> int:
    """The ``Retry-After`` that belongs to this refused client, in whole seconds.

    Capped at ``RATE_LIMIT_RETRY_AFTER_MAX_SECONDS``: a bucket that fills per hour would
    otherwise give a wait in which nobody sees a client come back.
    """
    ceiling = settings.RATE_LIMIT_RETRY_AFTER_MAX_SECONDS
    # Cap BEFORE the ceil: a limit of 0 gives math.inf, and math.ceil(inf) fails with an
    # OverflowError instead of the 429 this header belongs to.
    wait = min(limiter.seconds_until_token(client_id), float(ceiling))
    return min(max(math.ceil(wait), 1), ceiling)


def retry_after_header(limiter: IPRateLimiter, client_id: str) -> dict[str, str]:
    """The headers that go with a 429 from this limiter."""
    return {"Retry-After": str(retry_after_seconds(limiter, client_id))}


def _refuse_as_exhausted(limiter: IPRateLimiter, bucket: str, presented: str | None) -> NoReturn:
    """The 429 that belongs to an empty failure bucket."""
    logger.warning(f"Te veel mislukte authenticatiepogingen voor sleutel {fingerprint_for_log(presented or '')}")
    raise HTTPException(
        status_code=429,
        detail="Too many failed authentication attempts",
        headers=retry_after_header(limiter, bucket),
    )


def refuse_when_key_failures_exhausted(presented: str | None) -> None:
    """Refuse with a 429 as soon as the bucket of THIS presented key is empty.

    Only this bucket may refuse BEFORE the comparison: another party cannot empty it,
    because a wrong key has a different fingerprint and only failure counts down. The bucket
    per client refuses only after a failed comparison, so that the holder of a valid key is
    not locked out by a guesser on the same IP.
    """
    bucket = fingerprint_for_log(presented or "")
    if not AUTH_FAILURE_PER_KEY.has_tokens(bucket):
        _refuse_as_exhausted(AUTH_FAILURE_PER_KEY, bucket, presented)


def refuse_after_auth_failure(request: Request, presented: str | None) -> None:
    """Count the failed attempt down in both buckets, and refuse when the client bucket is empty.

    Belongs AFTER the key comparison: a valid key never passes here, so another party's
    failed attempts cost the holder nothing.
    """
    client = IPRateLimiter.get_client_identifier(request)
    AUTH_FAILURE_PER_KEY.is_allowed(fingerprint_for_log(presented or ""))
    if not AUTH_FAILURE_PER_CLIENT.is_allowed(client):
        _refuse_as_exhausted(AUTH_FAILURE_PER_CLIENT, client, presented)


#: Scope key: the coarse limit has already counted this request down. ``RateLimitMiddleware``
#: then does not count it again, because counting twice halves a pipeline's limit.
FLOOD_COUNTED_IN_SCOPE = "opi.flood_limit_counted"


def refuse_when_flood_limit_reached(request: Request) -> None:
    """Count the request down in the coarse limit, and refuse with a 429 when its bucket is empty.

    For a path that runs around ``RateLimitMiddleware``: the image push's pre-check refuses
    before the body and so sits outside that middleware. Whoever calls this sets
    ``FLOOD_COUNTED_IN_SCOPE``, so the request is not counted twice.
    """
    client_id = IPRateLimiter.get_client_identifier(request)
    if not API_FLOOD_LIMITER.is_allowed(client_id):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers=retry_after_header(API_FLOOD_LIMITER, client_id),
        )
