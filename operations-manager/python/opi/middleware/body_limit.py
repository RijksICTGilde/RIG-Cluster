"""Een bovengrens op de body van een upload, voordat die helemaal binnen is.

Een bijlage mag 64 KB zijn (``MAX_ATTACHMENT_BYTES``) en de routes lezen af op dat
plafond, maar de multipart-parser van Starlette heeft de body dan al ontvangen: boven
een MB spoolt hij naar schijf, en tot die grens staat alles in het geheugen. Deze
middleware weigert zo'n verzoek op de ontvangen bytes, zodat de pod dat niet draagt.

De grens is per route en niet app-breed: een image-tarball hoort veel hoger te mogen
(``IMAGE_UPLOAD_MAX_SIZE_MB``) dan een bijlage, en een route zonder upload houdt de
grens die Starlette zelf aanhoudt.

Voor de image-push doet deze middleware twee dingen meer, en wel hier omdat dit de enige
plek is die vóór de multipart-parser draait: de projectsleutel wordt gecontroleerd en het
uploadslot wordt genomen. Een 401 of een 503 uit de route zelf valt pas nadat de parser de
hele body heeft ontvangen, dus tien pushes van 5 GB zonder geldige sleutel zouden daar nog
50 GB tempschijf kosten.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from opi.api.endpoint_util import legitimeert
from opi.api.rate_limiter import refuse_after_auth_failure, refuse_when_key_failures_exhausted
from opi.core.config import settings
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES
from opi.utils.uploads import SLOT_HELD_IN_SCOPE, upload_slot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Het padsegment dat een bijlageroute kenmerkt, op beide wegen in: de wizard-upload
#: (``/forms/wizard/{flow_id}/attachments/...``) en de API-actie
#: (``/api/v2/projects/{project}/services/attachments/...``).
ATTACHMENT_PATH_SEGMENT = "/attachments"

#: Het padsegment van de image-push (``/api/v1/projects/{project}/images/push``).
IMAGE_PUSH_PATH_SEGMENT = "/images/push"

_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH"})

#: Dezelfde meldingen als ``validate_api_token``, want de controle is dezelfde.
_GEEN_SLEUTEL = "Authentication required - provide X-API-Key header"
_FOUTE_SLEUTEL = "Invalid API key"


def attachment_body_limit() -> int:
    """De bovengrens op de body van een bijlageroute, afgeleid van het bijlageplafond."""
    return MAX_ATTACHMENT_BYTES * settings.ATTACHMENT_BODY_LIMIT_FACTOR


def image_push_body_limit() -> int:
    """De bovengrens op de body van de image-push: zijn eigen plafond, in bytes."""
    return settings.IMAGE_UPLOAD_MAX_SIZE_MB * 1024 * 1024


def image_push_project(path: str) -> str | None:
    """De projectnaam uit een image-push-pad, of ``None`` als ``path`` er geen is."""
    if not path.endswith(IMAGE_PUSH_PATH_SEGMENT):
        return None
    return path[: -len(IMAGE_PUSH_PATH_SEGMENT)].rsplit("/", 1)[-1] or None


def body_limit_for(path: str) -> int | None:
    """De grens die voor ``path`` geldt, of ``None`` als deze middleware er niets doet."""
    if ATTACHMENT_PATH_SEGMENT in path:
        return attachment_body_limit()
    if path.endswith(IMAGE_PUSH_PATH_SEGMENT):
        return image_push_body_limit()
    return None


class BodyLimitMiddleware:
    """Weigert een upload met 413 zodra zijn body de grens van zijn route passeert."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        max_bytes = self._limit(scope)
        if max_bytes is None:
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > max_bytes:
            await self._refuse(scope, receive, send, max_bytes)
            return

        project_name = image_push_project(scope.get("path", ""))
        if project_name is not None:
            await self._image_push(scope, receive, send, max_bytes, project_name)
            return

        await self._count_while_reading(scope, receive, send, max_bytes)

    async def _image_push(self, scope: Scope, receive: Receive, send: Send, max_bytes: int, project_name: str) -> None:
        """Sleutel en slot vóór de parser, want een tarball van gigabytes kost tempschijf.

        Het slot blijft tot het antwoord verstuurd is, zodat het aantal tarballs op de
        tempschijf echt begrensd is en niet alleen het aantal dat tegelijk gelezen wordt.
        """
        weigering = await self._key_refusal(scope, project_name)
        if weigering is not None:
            await self._respond(scope, receive, send, *weigering)
            return

        genomen = False
        try:
            async with upload_slot():
                genomen = True
                scope[SLOT_HELD_IN_SCOPE] = True
                await self._count_while_reading(scope, receive, send, max_bytes)
        except HTTPException as bezet:
            # Alleen de 503 van het slot zelf, niet een uitzondering uit de app erachter.
            if genomen:
                raise
            await self._respond(scope, receive, send, bezet.status_code, str(bezet.detail), bezet.headers)

    async def _key_refusal(self, scope: Scope, project_name: str) -> tuple[int, str, Mapping[str, str] | None] | None:
        """Het antwoord dat de sleutelcontrole eist, of ``None`` als het bewijsmiddel klopt.

        Dezelfde twee faaltellers als ``validate_api_token``: deze weg loopt buiten
        ``RateLimitMiddleware`` om, dus zonder dit zou raden hier ongeremd zijn. Welk
        bewijsmiddel geldt beslist ``legitimeert``, zodat een token hier even ver komt als
        de oude projectsleutel en de twee niet uiteen kunnen gaan lopen.
        """
        presented = _header(scope, b"x-api-key")
        detail = _FOUTE_SLEUTEL if presented else _GEEN_SLEUTEL
        try:
            refuse_when_key_failures_exhausted(presented)
            if await legitimeert(presented, project_name):
                return None
            # Dezelfde WARNING als validate_api_token, die deze 401 voor is.
            logger.warning("Authentication failed for image push to %s: %s", project_name, detail)
            refuse_after_auth_failure(Request(scope), presented)
        except HTTPException as geweigerd:
            return geweigerd.status_code, str(geweigerd.detail), geweigerd.headers
        return 401, detail, None

    async def _count_while_reading(self, scope: Scope, receive: Receive, send: Send, max_bytes: int) -> None:
        """Chunked transfer kent geen Content-Length, dus daar valt de grens pas tijdens het lezen."""
        received = 0
        too_large = False
        responding = False

        async def counting_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    # Disconnect, geen exception: die zou de BaseHTTPMiddleware-taakgroep
                    # niet overleven.
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal responding
            if too_large and not responding:
                return
            responding = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            # Alleen wat op onze eigen disconnect volgt slikken we in: een ClientDisconnect
            # in de CSRF-middleware die de body leest, of een halve multipart in de parser.
            if not too_large or responding:
                raise
        if too_large and not responding:
            await self._refuse(scope, receive, send, max_bytes)

    def _limit(self, scope: Scope) -> int | None:
        if scope["type"] != "http" or scope.get("method", "") not in _METHODS_WITH_BODY:
            return None
        return body_limit_for(scope.get("path", ""))

    async def _refuse(self, scope: Scope, receive: Receive, send: Send, max_bytes: int) -> None:
        await self._respond(scope, receive, send, 413, _too_large_detail(max_bytes))

    async def _respond(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        response = JSONResponse({"detail": detail}, status_code=status_code, headers=headers)
        await response(scope, receive, send)


def _too_large_detail(max_bytes: int) -> str:
    """De melding bij een body boven de bovengrens, in KB."""
    return f"Body te groot (max {max_bytes // 1024} KB)"


def _header(scope: Scope, name: bytes) -> str | None:
    """De waarde van header ``name``, of ``None`` als hij er niet op staat."""
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def _declared_length(scope: Scope) -> int | None:
    """De ``Content-Length`` van het verzoek, of ``None`` als hij er niet of fout op staat."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
