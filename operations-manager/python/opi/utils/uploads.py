"""Reading an upload with a ceiling on it, and no more than a few at a time.

``await file.read()`` without an argument puts the whole file in memory before anything can
be checked, so an upload of a gigabyte costs a gigabyte even when the ceiling is 64 KB.
These readers stop on the first chunk that passes the ceiling.

Three readers on the same count: ``iter_capped`` for a caller that handles the chunks
itself, ``read_capped`` for one that wants the bytes in memory, and ``spill_capped`` for one
that wants to keep them in memory only while there are few of them.

``upload_slot`` additionally caps how many uploads run at the same time. Without that
ceiling, enough concurrent uploads can drain the pod's memory and temp disk together, each
one neatly below its own ceiling. Above that ceiling an upload waits in a bounded queue;
only when that is full does a 503 follow.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from opi.core.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, MutableMapping

    from fastapi import UploadFile

#: Read chunk, shared by every upload route.
CHUNK_SIZE = 64 * 1024


class UploadTooLargeError(Exception):
    """An upload that passes the ceiling it was given."""


async def iter_capped(upload: UploadFile, max_bytes: int) -> AsyncIterator[bytes]:
    """The chunks of ``upload``, up to and including the ceiling.

    Raises ``UploadTooLargeError`` on the chunk that passes ``max_bytes``; what sits behind
    it is not read at all.
    """
    total = 0
    while chunk := await upload.read(CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLargeError(max_bytes)
        yield chunk


async def read_capped(upload: UploadFile, max_bytes: int) -> bytes | None:
    """The bytes of ``upload``, or ``None`` the moment it carries more than ``max_bytes``."""
    try:
        return b"".join([chunk async for chunk in iter_capped(upload, max_bytes)])
    except UploadTooLargeError:
        return None


@dataclass
class CappedUpload:
    """A read upload: ``content`` while it fitted in memory, otherwise ``path``."""

    size: int
    content: bytes | None = None
    path: str | None = None

    def cleanup(self) -> None:
        """Remove the temp file. A no-op for an upload that stayed in memory."""
        if self.path:
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path)
            self.path = None


async def spill_capped(upload: UploadFile, max_bytes: int, *, spill_after: int | None = None) -> CappedUpload | None:
    """``upload`` in memory up to ``spill_after`` bytes, above that into a temp file.

    Returns ``None`` the moment the upload passes ``max_bytes``; a half-written temp file is
    cleaned up by then. The caller removes the file with ``cleanup()``.
    """
    if spill_after is None:
        spill_after = settings.UPLOAD_SPILL_AFTER_BYTES

    buffer = bytearray()
    staged = CappedUpload(size=0)
    handle = None
    try:
        async for chunk in iter_capped(upload, max_bytes):
            staged.size += len(chunk)
            if handle is None and staged.size > spill_after:
                # Mode 0o600, as in the staging store: what passes here can be a private
                # key.
                fd, staged.path = tempfile.mkstemp(dir=settings.TEMP_DIR)
                os.fchmod(fd, 0o600)
                handle = os.fdopen(fd, "wb")
                handle.write(buffer)
                buffer.clear()
            if handle is None:
                buffer.extend(chunk)
            else:
                handle.write(chunk)
    except UploadTooLargeError:
        staged.cleanup()
        return None
    except BaseException:
        # An aborted upload leaves nothing behind either. A client that drops out gives a
        # CancelledError, and that is not an Exception.
        staged.cleanup()
        raise
    finally:
        if handle is not None:
            handle.close()

    if staged.path is None:
        staged.content = bytes(buffer)
    return staged


def too_large_detail(max_bytes: int) -> str:
    """The message for a file above the ceiling, in KB."""
    return f"Bestand te groot (max {max_bytes // 1024} KB)"


_slot: asyncio.Semaphore | None = None
_slot_size = 0
_waiting = 0


def _upload_semaphore() -> asyncio.Semaphore:
    """The lock on the number of concurrent uploads.

    OPI runs one process without extra workers, so this is a truly global ceiling. It is
    rebuilt the moment the setting holds another value, so a test can move it; in production
    that value is fixed after the start.
    """
    global _slot, _slot_size
    if _slot is None or _slot_size != settings.MAX_CONCURRENT_UPLOADS:
        _slot = asyncio.Semaphore(settings.MAX_CONCURRENT_UPLOADS)
        _slot_size = settings.MAX_CONCURRENT_UPLOADS
    return _slot


def _too_busy() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Te veel uploads tegelijk, probeer het over een paar seconden opnieuw",
        headers={"Retry-After": str(settings.UPLOAD_SLOT_RETRY_AFTER_SECONDS)},
    )


async def _take_slot(semaphore: asyncio.Semaphore) -> None:
    """A place in the lock, possibly after a queue ``UPLOAD_QUEUE_SIZE`` deep."""
    global _waiting
    if not semaphore.locked():
        await semaphore.acquire()
        return
    if _waiting >= settings.UPLOAD_QUEUE_SIZE:
        raise _too_busy()
    _waiting += 1
    try:
        async with asyncio.timeout(settings.UPLOAD_QUEUE_WAIT_SECONDS):
            await semaphore.acquire()
    except TimeoutError:
        raise _too_busy() from None
    finally:
        _waiting -= 1


#: Marker in the ASGI scope: this request already holds a place in the lock.
SLOT_HELD_IN_SCOPE = "zad_upload_slot_held"


@asynccontextmanager
async def upload_slot(scope: MutableMapping[str, Any] | None = None) -> AsyncIterator[None]:
    """Take a place in the upload lock, through the queue when needed.

    When ``scope`` carries the marker of ``BodyLimitMiddleware`` this is a no-op: that one
    took the place before the multipart parser and holds it until the response.

    An upload is usually read in a blink, so waiting for a place to come free beats refusing
    straight away. But that queue is itself bounded: every waiting request holds on to its
    connection and its stream, and an unbounded queue is exactly the pile-up this ceiling is
    meant to prevent. When the queue is full, or a request sits in it longer than
    ``UPLOAD_QUEUE_WAIT_SECONDS``, a 503 with a ``Retry-After`` follows.
    """
    if scope is not None and scope.get(SLOT_HELD_IN_SCOPE):
        yield
        return

    semaphore = _upload_semaphore()
    await _take_slot(semaphore)
    try:
        yield
    finally:
        semaphore.release()
