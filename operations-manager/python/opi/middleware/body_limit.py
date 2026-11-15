"""An upper bound on an upload's body, before it is fully in.

For the image push this middleware also weighs the role before FastAPI reads the body,
so a refusal costs no temp disk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from opi.core.audit_trail import begin_trail
from opi.core.config import settings
from opi.http.auth.endpoint_util import image_push_refusal
from opi.http.rate_limiter import (
    FLOOD_COUNTED_IN_SCOPE,
    refuse_when_flood_limit_reached,
    refuse_when_key_failures_exhausted,
)
from opi.middleware.audit import queue_audit_rows
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES
from opi.utils.uploads import SLOT_HELD_IN_SCOPE, upload_slot

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: The path segment that marks an attachment route, on both ways in: the wizard upload
#: (``/forms/wizard/{flow_id}/attachments/...``) and the API action
#: (``/api/v2/projects/{project}/services/attachments/...``).
ATTACHMENT_PATH_SEGMENT = "/attachments"

#: The path segment of the image push (``/api/v1/projects/{project}/images/push``).
IMAGE_PUSH_PATH_SEGMENT = "/images/push"

_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH"})


def attachment_body_limit() -> int:
    return MAX_ATTACHMENT_BYTES * settings.ATTACHMENT_BODY_LIMIT_FACTOR


def image_push_body_limit() -> int:
    return settings.IMAGE_UPLOAD_MAX_SIZE_MB * 1024 * 1024


def image_push_project(path: str) -> str | None:
    """The project name from an image-push path, or ``None`` when ``path`` is not one."""
    if not path.endswith(IMAGE_PUSH_PATH_SEGMENT):
        return None
    return path[: -len(IMAGE_PUSH_PATH_SEGMENT)].rsplit("/", 1)[-1] or None


def body_limit_for(path: str) -> int | None:
    """The limit that applies to ``path``, or ``None`` when this middleware does nothing there."""
    if ATTACHMENT_PATH_SEGMENT in path:
        return attachment_body_limit()
    if path.endswith(IMAGE_PUSH_PATH_SEGMENT):
        return image_push_body_limit()
    return None


async def _no_body() -> Message:
    """A zero-byte body, for a request that may not be read yet."""
    return {"type": "http.request", "body": b"", "more_body": False}


class BodyLimitMiddleware:
    """Refuses an upload with 413 as soon as its body passes the limit of its route."""

    def __init__(self, app: ASGIApp, *, image_push_endpoint: Callable[..., Any]) -> None:
        self.app = app
        # The route's own endpoint object, so the pre-check here weighs the action that the
        # route carries. Passed in by the composition root: an import would point upward.
        self.image_push_endpoint = image_push_endpoint

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
        """Key, role and slot before the parser: a tarball of gigabytes costs temp disk.

        The slot is held until the response is sent, so the number of tarballs on the temp
        disk is really bounded and not just the number being read at the same time.
        """
        refusal = await self._push_refusal(scope, project_name)
        if refusal is not None:
            await self._respond(scope, receive, send, *refusal)
            return

        held = False
        try:
            async with upload_slot():
                held = True
                scope[SLOT_HELD_IN_SCOPE] = True
                await self._count_while_reading(scope, receive, send, max_bytes)
        except HTTPException as busy:
            # Only the 503 of the slot itself, not an exception from the app behind it.
            if held:
                raise
            await self._respond(scope, receive, send, busy.status_code, str(busy.detail), busy.headers)

    async def _push_refusal(
        self, scope: Scope, project_name: str
    ) -> tuple[int, str | dict[str, str], Mapping[str, str] | None] | None:
        """Check the role; returns the refusal response, or ``None`` when allowed."""
        presented = _header(scope, b"x-api-key")
        # Empty receive: headers and query are already on scope, the body may not be read yet.
        request = Request(scope, receive=_no_body)
        begin_trail()
        try:
            refuse_when_flood_limit_reached(request)
            scope[FLOOD_COUNTED_IN_SCOPE] = True
            refuse_when_key_failures_exhausted(presented)
            refusal = await image_push_refusal(request, presented, project_name, self.image_push_endpoint)
        except HTTPException as refused:
            return refused.status_code, str(refused.detail), refused.headers
        if refusal is None:
            return None
        queue_audit_rows(request, refusal.status_code)
        detail = refusal.detail if isinstance(refusal.detail, dict) else str(refusal.detail)
        return refusal.status_code, detail, refusal.headers

    async def _count_while_reading(self, scope: Scope, receive: Receive, send: Send, max_bytes: int) -> None:
        """Chunked transfer has no Content-Length, so there the limit only falls while reading."""
        received = 0
        too_large = False
        responding = False

        async def counting_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    # Disconnect, not an exception: that would not survive the
                    # BaseHTTPMiddleware task group.
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
            # Only what follows our own disconnect is swallowed: a ClientDisconnect in the
            # CSRF middleware that reads the body, or half a multipart in the parser.
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
    return f"Body te groot (max {max_bytes // 1024} KB)"


def _header(scope: Scope, name: bytes) -> str | None:
    """The value of header ``name``, or ``None`` when it is not on the request."""
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def _declared_length(scope: Scope) -> int | None:
    """The request's ``Content-Length``, or ``None`` when it is absent or malformed."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
