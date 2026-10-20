"""Een bovengrens op de body van een bijlage-upload, voordat die helemaal binnen is.

Een bijlage mag 64 KB zijn (``MAX_ATTACHMENT_BYTES``) en de routes lezen af op dat
plafond, maar de multipart-parser van Starlette heeft de body dan al ontvangen: boven
een MB spoolt hij naar schijf, en tot die grens staat alles in het geheugen. Deze
middleware weigert zo'n verzoek op de ontvangen bytes, zodat de pod dat niet draagt.

Niet app-breed: de image-upload heeft een eigen, veel hoger plafond
(``IMAGE_UPLOAD_MAX_SIZE_MB``) en streamt naar een tempbestand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Het padsegment dat een bijlageroute kenmerkt, op beide wegen in: de wizard-upload
#: (``/forms/wizard/{flow_id}/attachments/...``) en de API-actie
#: (``/api/v2/projects/{project}/services/attachments/...``).
ATTACHMENT_PATH_SEGMENT = "/attachments"

#: Ruim boven de 64 KB van een bijlage plus de multipart-overhead eromheen, en ruim onder
#: wat de pod aan kan. Een verzoek hierboven is geen bijlage die net te groot is.
MAX_ATTACHMENT_BODY_BYTES = 1024 * 1024

_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH"})


class BodyLimitMiddleware:
    """Weigert een bijlage-upload met 413 zodra zijn body de grens passeert."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_ATTACHMENT_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._limits(scope):
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._refuse(scope, receive, send)
            return

        await self._count_while_reading(scope, receive, send)

    async def _count_while_reading(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Chunked transfer kent geen Content-Length, dus daar valt de grens pas tijdens het lezen."""
        received = 0
        too_large = False
        responding = False

        async def counting_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Een disconnect in plaats van een uitzondering: die komt niet heel
                    # door de stack (de taakgroep van een BaseHTTPMiddleware verpakt hem, en
                    # FastAPI maakt van request.form() dan een 400). Wij antwoorden zelf.
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
            await self._refuse(scope, receive, send)

    def _limits(self, scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("method", "") in _METHODS_WITH_BODY
            and ATTACHMENT_PATH_SEGMENT in scope.get("path", "")
        )

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": _too_large_detail(self.max_bytes)}, status_code=413)
        await response(scope, receive, send)


def _too_large_detail(max_bytes: int) -> str:
    """De melding bij een body boven de bovengrens, in KB."""
    return f"Body te groot (max {max_bytes // 1024} KB)"


def _declared_length(scope: Scope) -> int | None:
    """De ``Content-Length`` van het verzoek, of ``None`` als hij er niet of fout op staat."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
