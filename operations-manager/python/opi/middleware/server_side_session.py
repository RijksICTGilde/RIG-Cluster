"""Server-side sessions behind a small cookie.

The cookie carries only a signed session id (itsdangerous); the content lives in the
``sessions`` table via :class:`~opi.services.session_store.SessionStore`, with the hot
cache in front of it.

``request.session`` behaves like Starlette's SessionMiddleware: a dict. Two differences,
both deliberate:

- a mutation is written out at once (write-through), so a later reader sees the change
  halfway through the request; the middleware waits for the pending writes before the
  response leaves;
- static paths and probes do not read the session at all (the same exclusion list as the
  authorization middleware): no database or cache traffic for /health and /static.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import MutableMapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal, Protocol

from itsdangerous import BadSignature, TimestampSigner
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection

from opi.middleware.authorization import SKIP_AUTH_EXACT, SKIP_AUTH_PREFIXES
from opi.services.session_store import get_session_store

if TYPE_CHECKING:
    from concurrent.futures import Future

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class SessionBackend(Protocol):
    """What the middleware uses from the session storage.

    Apart from ``SessionStore`` itself, so the unit tests and the local e2e harness can plug
    in an in-memory stand-in without inheriting from the class.
    """

    async def get(self, session_id: str) -> dict[str, Any] | None: ...
    async def set(self, session_id: str, data: dict[str, Any]) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def touch(self, session_id: str) -> bool: ...


class ServerSideSession(MutableMapping[str, Any]):
    """The request session as a dict, with write-through to the store.

    Reads from the data loaded at the start; every mutation updates that data and schedules a
    ``store.set`` at once (the full dict, so the row follows the last mutation). The writes run
    on the application loop, also from a sync route on a threadpool. ``flush`` on sending the
    response waits for them.
    """

    def __init__(
        self,
        store: SessionBackend,
        session_id: str,
        data: dict[str, Any],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._store = store
        self.session_id = session_id
        self._data = data
        self._loop = loop
        self._pending: set[Future[None]] = set()
        self._last_write: Future[None] | None = None
        self.accessed = False
        self.modified = False

    def __getitem__(self, key: str) -> Any:
        self.accessed = True
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.accessed = True
        self.modified = True
        self._data[key] = value
        self._write_through()

    def __delitem__(self, key: str) -> None:
        self.accessed = True
        self.modified = True
        del self._data[key]
        self._write_through()

    def __iter__(self) -> Any:
        self.accessed = True
        return iter(self._data)

    def __len__(self) -> int:
        self.accessed = True
        return len(self._data)

    def _write_through(self) -> None:
        """Schedule the persist of the current state without making the caller wait."""
        snapshot = deepcopy(self._data)
        session_id = self.session_id
        previous = self._last_write
        store = self._store

        async def _persist() -> None:
            # Each upsert is its own transaction; unchained, an earlier snapshot could commit
            # last and the row would lose the later mutations (id_token after login). A failed
            # predecessor is already logged by its own callback, and must not block this write.
            if previous is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wrap_future(previous)
            await store.set(session_id, snapshot)

        future = asyncio.run_coroutine_threadsafe(_persist(), self._loop)
        self._last_write = future
        self._pending.add(future)

        def _log_failure(f: Future[None]) -> None:
            if f.cancelled():
                return
            if exc := f.exception():
                logger.error("Persisting session %s... failed: %s", self.session_id[:12], exc, exc_info=exc)

        future.add_done_callback(_log_failure)
        future.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        """Wait for the pending writes; failures are already logged by the callback."""
        while self._pending:
            futures = [asyncio.wrap_future(f) for f in self._pending]
            with contextlib.suppress(Exception):
                await asyncio.gather(*futures)

    async def regenerate(self) -> None:
        """New session id on a privilege hinge: the row and the cache entry behind the old id
        fall away, and the mutation makes the response set the cookie to the new id."""
        await self.flush()
        await self._store.delete(self.session_id)
        self.session_id = secrets.token_urlsafe(32)
        self.modified = True
        self._write_through()


class ServerSideSessionMiddleware:
    """Replaces Starlette's SessionMiddleware: content server-side, id in the cookie.

    The same conventions as Starlette: ``Vary: Cookie`` once the session has been read, a fresh
    cookie signature on every mutation, and an explicit expiry of the cookie when a filled
    session is emptied. The difference: the cookie carries no data but the signed id, and the
    lifetime of the content belongs to the store (a logout or a revocation deletes the row and
    so gives a hard refusal on the next request). The row's expiry moves along with activity
    via ``touch``, so the idle window does not hit an active user.
    """

    def __init__(
        self,
        app: ASGIApp,
        secret_key: str,
        session_cookie: str = "session",
        max_age: int | None = 14 * 24 * 60 * 60,
        path: str = "/",
        same_site: Literal["lax", "strict", "none"] = "lax",
        https_only: bool = False,
        store: SessionBackend | None = None,
    ) -> None:
        self.app = app
        self.signer = TimestampSigner(secret_key)
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.path = path
        self.security_flags = "httponly; samesite=" + same_site
        if https_only:
            self.security_flags += "; secure"
        self._store = store

    def _session_store(self) -> SessionBackend:
        """Resolved per request, so tests and the e2e harness can patch the store."""
        return self._store or get_session_store()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        loop = asyncio.get_running_loop()
        store = self._session_store()

        session_id: str | None = None
        if self._skip_session_read(scope):
            # Static paths and probes (AMT/TAD): no store traffic, no cookie actions. A writer
            # here writes into a throwaway dict, deliberately: nothing on these paths holds a
            # session.
            scope["session"] = {}
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        if self.session_cookie in connection.cookies:
            raw_value = connection.cookies[self.session_cookie].encode("utf-8")
            try:
                # max_age enforces the idle window here, on the signature, not on the cookie.
                session_id = self.signer.unsign(raw_value, max_age=self.max_age).decode("utf-8")
            except BadSignature:
                logger.warning("Invalid session cookie signature - tampering or SECRET_KEY mismatch")

        data: dict[str, Any] = {}
        if session_id:
            data = await store.get(session_id) or {}
            if data and scope["type"] == "http":
                # The sliding window of the row moves along with activity.
                await store.touch(session_id)

        session = ServerSideSession(store, session_id or secrets.token_urlsafe(32), data, loop)
        scope["session"] = session
        had_data = bool(data)

        if scope["type"] == "websocket":
            # A handshake cannot set or refresh a cookie; the session is read-only material
            # for the endpoint itself here.
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                await session.flush()
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                if session.modified and len(session):
                    # A fresh signature only on a mutation: the cookie is then back on "now".
                    signed = self.signer.sign(session.session_id.encode("utf-8"))
                    # No Max-Age: a session cookie, gone when the browser closes.
                    header_value = "{session_cookie}={data}; path={path}; {security_flags}".format(
                        session_cookie=self.session_cookie,
                        data=signed.decode("utf-8"),
                        path=self.path,
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
                elif session.modified and had_data:
                    # The session was emptied: the row goes, and so does the browser cookie.
                    await store.delete(session.session_id)
                    header_value = "{session_cookie}={data}; path={path}; {expires}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data="null",
                        path=self.path,
                        expires="expires=Thu, 01 Jan 1970 00:00:00 GMT; ",
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _skip_session_read(scope: Scope) -> bool:
        """Static paths and probes do not read the session: no store traffic.

        The same exclusion as the authorization middleware: what has no user context there
        needs no session content here.
        """
        path: str = scope.get("path", "")
        return path in SKIP_AUTH_EXACT or path.startswith(SKIP_AUTH_PREFIXES)
