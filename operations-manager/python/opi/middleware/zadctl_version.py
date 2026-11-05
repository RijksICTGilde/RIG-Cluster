"""Weigert een te oude zadctl met 426, en waarschuwt een verouderde met een header.

Zie ``opi/core/zadctl_version.py`` voor de afspraak. Staat buiten sessie en autorisatie:
een client die toch geweigerd wordt, hoeft zijn sleutel niet eerst te laten valideren.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from opi.core.zadctl_version import UPGRADE_URL, parse_setting, version_from_user_agent

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

MINIMUM_HEADER = "X-ZADCTL-Minimum"
RECOMMENDED_HEADER = "X-ZADCTL-Recommended"


class ZadctlVersionMiddleware:
    """Toetst de zadctl-versie uit de User-Agent aan de grenzen van dit cluster."""

    def __init__(self, app: ASGIApp, minimum: str = "", recommended: str = "") -> None:
        self.app = app
        self.minimum = minimum
        self.recommended = recommended
        self._minimum = parse_setting("ZADCTL_MIN_VERSION", minimum)
        self._recommended = parse_setting("ZADCTL_RECOMMENDED_VERSION", recommended)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        user_agent = Headers(scope=scope).get("user-agent", "")
        version = version_from_user_agent(user_agent)
        if version is None:
            await self.app(scope, receive, send)
            return

        if self._minimum and version < self._minimum:
            sent = ".".join(str(part) for part in version)
            response = JSONResponse(
                {
                    "detail": (
                        f"zadctl {sent} werkt niet meer met deze API. Installeer minstens versie {self.minimum}."
                    ),
                    "client": "zadctl",
                    "version": sent,
                    "minimum": self.minimum,
                    "upgrade": UPGRADE_URL,
                },
                status_code=426,
                headers={MINIMUM_HEADER: self.minimum},
            )
            await response(scope, receive, send)
            return

        if not (self._recommended and version < self._recommended):
            await self.app(scope, receive, send)
            return

        async def send_with_advice(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[RECOMMENDED_HEADER] = self.recommended
            await send(message)

        await self.app(scope, receive, send_with_advice)
