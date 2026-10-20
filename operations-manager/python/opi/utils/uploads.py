"""Een upload lezen met een plafond erop.

``await file.read()`` zonder argument zet het hele bestand in het geheugen voordat er
iets getoetst kan worden, dus kost een upload van een gigabyte een gigabyte, ook als het
plafond 64 KB is. Deze lezers breken af op het eerste blok dat het plafond passeert.

``iter_capped`` is de lezer voor wie de blokken zelf verwerkt (de image-upload schrijft
ze naar een tempbestand), ``read_capped`` die voor wie de bytes in het geheugen wil.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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


def too_large_detail(max_bytes: int) -> str:
    """De melding bij een bestand boven het plafond, in KB."""
    return f"Bestand te groot (max {max_bytes // 1024} KB)"
