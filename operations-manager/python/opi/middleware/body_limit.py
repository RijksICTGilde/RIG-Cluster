"""Een bovengrens op de body van een upload, voordat die helemaal binnen is.

Voor de image-push weegt deze middleware ook de rol voordat FastAPI de body leest,
zodat een weigering geen tempschijf kost.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from opi.api.endpoint_util import image_push_refusal
from opi.api.image_router import push_image
from opi.api.rate_limiter import (
    FLOOD_COUNTED_IN_SCOPE,
    refuse_when_flood_limit_reached,
    refuse_when_key_failures_exhausted,
)
from opi.core.audit_trail import begin_trail
from opi.core.config import settings
from opi.middleware.audit import zet_rijen_in_de_wachtrij
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


async def _geen_body() -> Message:
    """Een body van nul bytes, voor een verzoek dat nog niet gelezen mag worden."""
    return {"type": "http.request", "body": b"", "more_body": False}


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
        """Sleutel, rol en slot vóór de parser, want een tarball van gigabytes kost tempschijf.

        Het slot blijft tot het antwoord verstuurd is, zodat het aantal tarballs op de
        tempschijf echt begrensd is en niet alleen het aantal dat tegelijk gelezen wordt.
        """
        weigering = await self._push_refusal(scope, project_name)
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

    async def _push_refusal(
        self, scope: Scope, project_name: str
    ) -> tuple[int, str | dict[str, str], Mapping[str, str] | None] | None:
        """Controleer de rol en return antwoord als weigering, of ``None`` als toegestaan."""
        presented = _header(scope, b"x-api-key")
        # Lege receive: headers en query zijn al op scope, body mag nog niet gelezen worden.
        request = Request(scope, receive=_geen_body)
        begin_trail()
        try:
            refuse_when_flood_limit_reached(request)
            scope[FLOOD_COUNTED_IN_SCOPE] = True
            refuse_when_key_failures_exhausted(presented)
            weigering = await image_push_refusal(request, presented, project_name, push_image)
        except HTTPException as geweigerd:
            return geweigerd.status_code, str(geweigerd.detail), geweigerd.headers
        if weigering is None:
            return None
        zet_rijen_in_de_wachtrij(request, weigering.status_code)
        detail = weigering.detail if isinstance(weigering.detail, dict) else str(weigering.detail)
        return weigering.status_code, detail, weigering.headers

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
        detail: str | dict[str, str],
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
