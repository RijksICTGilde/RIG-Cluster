"""The request session as a dict, with write-through to the store.

Shared between the middleware that puts it on the request
(:class:`~opi.middleware.server_side_session.ServerSideSessionMiddleware`) and the login
route, which regenerates the session id on the privilege hinge.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import MutableMapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator
    from concurrent.futures import Future

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

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401  # a session value
        self.accessed = True
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:  # noqa: ANN401  # a session value
        self.accessed = True
        self.modified = True
        self._data[key] = value
        self._write_through()

    def __delitem__(self, key: str) -> None:
        self.accessed = True
        self.modified = True
        del self._data[key]
        self._write_through()

    def __iter__(self) -> Iterator[str]:
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
