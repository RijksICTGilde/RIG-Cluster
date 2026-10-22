"""Een upload lezen met een plafond erop, en niet meer dan een paar tegelijk.

``await file.read()`` zonder argument zet het hele bestand in het geheugen voordat er
iets getoetst kan worden, dus kost een upload van een gigabyte een gigabyte, ook als het
plafond 64 KB is. Deze lezers breken af op het eerste blok dat het plafond passeert.

Drie lezers op dezelfde telling: ``iter_capped`` voor wie de blokken zelf verwerkt,
``read_capped`` voor wie de bytes in het geheugen wil, en ``spill_capped`` voor wie ze
alleen in het geheugen wil houden zolang het er weinig zijn.

``upload_slot`` begrenst daarnaast hoeveel uploads er tegelijk lopen. Zonder dat plafond
kunnen genoeg gelijktijdige uploads het geheugen en de tempschijf van de pod samen
leegtrekken, elk netjes onder zijn eigen plafond. Boven dat plafond wacht een upload in
een begrensde wachtrij; pas als die vol is volgt een 503.
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

#: Leesblok, gedeeld door alle uploadroutes.
CHUNK_SIZE = 64 * 1024


class UploadTooLargeError(Exception):
    """Een upload die het meegegeven plafond passeert."""


async def iter_capped(upload: UploadFile, max_bytes: int) -> AsyncIterator[bytes]:
    """De blokken van ``upload``, tot en met het plafond.

    Gooit ``UploadTooLargeError`` op het blok dat ``max_bytes`` passeert; wat erachter
    zit wordt niet meer gelezen.
    """
    total = 0
    while chunk := await upload.read(CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLargeError(max_bytes)
        yield chunk


async def read_capped(upload: UploadFile, max_bytes: int) -> bytes | None:
    """De bytes van ``upload``, of ``None`` zodra hij meer dan ``max_bytes`` draagt."""
    try:
        return b"".join([chunk async for chunk in iter_capped(upload, max_bytes)])
    except UploadTooLargeError:
        return None


@dataclass
class CappedUpload:
    """Een gelezen upload: ``content`` zolang hij in het geheugen paste, anders ``path``."""

    size: int
    content: bytes | None = None
    path: str | None = None

    def cleanup(self) -> None:
        """Ruimt het tempbestand op. Geen bewerking voor een upload die in het geheugen bleef."""
        if self.path:
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path)
            self.path = None


async def spill_capped(upload: UploadFile, max_bytes: int, *, spill_after: int | None = None) -> CappedUpload | None:
    """``upload`` in het geheugen tot ``spill_after`` bytes, daarboven naar een tempbestand.

    Geeft ``None`` zodra de upload ``max_bytes`` passeert; een half weggeschreven
    tempbestand is dan al opgeruimd. De beller ruimt het bestand op met ``cleanup()``.
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
                # Mode 0o600, net als de staging-store: wat hier langskomt kan een
                # private key zijn.
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
        # Ook een afgebroken upload laat niets achter. Een client die wegvalt geeft een
        # CancelledError, en dat is geen Exception.
        staged.cleanup()
        raise
    finally:
        if handle is not None:
            handle.close()

    if staged.path is None:
        staged.content = bytes(buffer)
    return staged


def too_large_detail(max_bytes: int) -> str:
    """De melding bij een bestand boven het plafond, in KB."""
    return f"Bestand te groot (max {max_bytes // 1024} KB)"


_slot: asyncio.Semaphore | None = None
_slot_size = 0
_wachtend = 0


def _upload_semaphore() -> asyncio.Semaphore:
    """Het slot op het aantal gelijktijdige uploads.

    OPI draait één proces zonder extra workers, dus dit is een echt globaal plafond. Hij
    wordt opnieuw gemaakt zodra de setting een andere waarde heeft, zodat een test hem
    kan verzetten; in productie staat die waarde vast na het starten.
    """
    global _slot, _slot_size
    if _slot is None or _slot_size != settings.MAX_CONCURRENT_UPLOADS:
        _slot = asyncio.Semaphore(settings.MAX_CONCURRENT_UPLOADS)
        _slot_size = settings.MAX_CONCURRENT_UPLOADS
    return _slot


def _bezet() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Te veel uploads tegelijk, probeer het over een paar seconden opnieuw",
        headers={"Retry-After": str(settings.UPLOAD_SLOT_RETRY_AFTER_SECONDS)},
    )


async def _neem_plek(semaphore: asyncio.Semaphore) -> None:
    """Een plek in het slot, eventueel na een wachtrij van ``UPLOAD_QUEUE_SIZE`` diep."""
    global _wachtend
    if not semaphore.locked():
        await semaphore.acquire()
        return
    if _wachtend >= settings.UPLOAD_QUEUE_SIZE:
        raise _bezet()
    _wachtend += 1
    try:
        async with asyncio.timeout(settings.UPLOAD_QUEUE_WAIT_SECONDS):
            await semaphore.acquire()
    except TimeoutError:
        raise _bezet() from None
    finally:
        _wachtend -= 1


#: Markering in de ASGI-scope: het slot is voor dit verzoek al genomen.
SLOT_HELD_IN_SCOPE = "zad_upload_slot_held"


@asynccontextmanager
async def upload_slot(scope: MutableMapping[str, Any] | None = None) -> AsyncIterator[None]:
    """Neemt een plek in het uploadslot, zo nodig via de wachtrij.

    Draagt ``scope`` de markering van ``BodyLimitMiddleware``, dan is dit geen bewerking:
    die heeft de plek al vóór de multipart-parser genomen en houdt hem tot het antwoord.

    Een upload is meestal in een oogwenk gelezen, dus wachten tot er een plek vrijkomt is
    beter dan meteen weigeren. Maar die wachtrij is zelf begrensd: elk wachtend verzoek
    houdt zijn verbinding en zijn stream vast, en een onbegrensde rij is weer precies de
    opstapeling die dit plafond moet voorkomen. Is de rij vol, of staat een verzoek er
    langer dan ``UPLOAD_QUEUE_WAIT_SECONDS`` in, dan volgt een 503 met een ``Retry-After``.
    """
    if scope is not None and scope.get(SLOT_HELD_IN_SCOPE):
        yield
        return

    semaphore = _upload_semaphore()
    await _neem_plek(semaphore)
    try:
        yield
    finally:
        semaphore.release()
