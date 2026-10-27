"""Server-side sessies achter een kleine cookie (RC-390).

De cookie draagt alleen een ondertekend sessie-id (itsdangerous); de inhoud woont in de
tabel ``sessions`` via :class:`~opi.services.session_store.SessionStore`, met de
hot-cache ervoor.

``request.session`` gedraagt zich als bij Starlette's SessionMiddleware: een dict. Twee
verschillen, beide bewust:

- een mutatie wordt direct weggeschreven (write-through), zodat een volgende lezer de
  wijziging halverwege het request al ziet; de middleware wacht de lopende schrijfacties
  af voordat het antwoord vertrekt;
- statische paden en probes lezen de sessie helemaal niet (dezelfde uitsluitlijst als de
  autorisatie-middleware): geen database- of cacheverkeer voor /health en /static.
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
    """Wat de middleware van de sessie-opslag gebruikt.

    Los van ``SessionStore`` zelf, zodat de unit-tests en het lokale e2e-harnas een
    in-memory stand-in kunnen inpluggen zonder aan de klasse te erven.
    """

    async def get(self, session_id: str) -> dict[str, Any] | None: ...
    async def set(self, session_id: str, data: dict[str, Any]) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def touch(self, session_id: str) -> bool: ...


class ServerSideSession(MutableMapping[str, Any]):
    """De request-sessie als dict, met write-through naar de store.

    Leest uit de bij de aanvang geladen data; elke mutatie werkt die data bij én plant
    direct een ``store.set`` (de volledige dict, dus de rij volgt de laatste mutatie).
    De schrijfacties draaien op de loop van de applicatie, ook vanuit een sync route die
    op een threadpool draait. ``flush`` bij het verzenden van het antwoord wacht ze af.
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
        """Plan de persist van de huidige stand, zonder de aanroeper te laten wachten."""
        snapshot = deepcopy(self._data)
        future = asyncio.run_coroutine_threadsafe(self._store.set(self.session_id, snapshot), self._loop)
        self._pending.add(future)

        def _log_failure(f: Future[None]) -> None:
            if f.cancelled():
                return
            if exc := f.exception():
                logger.error("Persisting session %s... failed: %s", self.session_id[:12], exc, exc_info=exc)

        future.add_done_callback(_log_failure)
        future.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        """Wacht de lopende schrijfacties af; fouten zijn al gelogd door de callback."""
        while self._pending:
            futures = [asyncio.wrap_future(f) for f in self._pending]
            with contextlib.suppress(Exception):
                await asyncio.gather(*futures)

    async def regenerate(self) -> None:
        """Nieuw sessie-id op een privilegescharnier: de rij en de cache-entry achter het
        oude id vallen weg, en de mutatie laat het antwoord de cookie op het nieuwe id zetten."""
        await self.flush()
        await self._store.delete(self.session_id)
        self.session_id = secrets.token_urlsafe(32)
        self.modified = True
        self._write_through()


class ServerSideSessionMiddleware:
    """Vervanger voor Starlette's SessionMiddleware: inhoud server-side, id in de cookie.

    Dezelfde afspraken als bij Starlette: ``Vary: Cookie`` zodra de sessie gelezen is,
    een verse cookie-ondertekening bij elke mutatie, en een expliciet vervallen van de
    cookie als een gevulde sessie leeggemaakt wordt. Verschil: de cookie draagt geen data
    maar het signede id, en de levensloop van de inhoud hoort bij de store
    (uitloggen en intrekken verwijderen de rij en geven zo een harde weigering op de
    volgende request). Het verloop van de rij schuift mee met activiteit via ``touch``,
    dus het inactiviteitsvenster treft geen actieve gebruiker.
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
        """Per request opgelost, zodat tests en het e2e-harnas de store kunnen patchen."""
        return self._store or get_session_store()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        loop = asyncio.get_running_loop()
        store = self._session_store()

        session_id: str | None = None
        if self._skip_session_read(scope):
            # Statische paden en probes (AMT/TAD): geen store-verkeer, geen cookie-acties.
            # Een schrijver hier schrijft in een wegwerp-dict - dezelfde stille leegstand
            # als bij AMT, en bewust: er is op deze paden niets dat een sessie bevat.
            scope["session"] = {}
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        if self.session_cookie in connection.cookies:
            raw_value = connection.cookies[self.session_cookie].encode("utf-8")
            try:
                # max_age handhaaft het inactiviteitsvenster op de ondertekening, zoals
                # Starlette dat op de cookie zelf deed.
                session_id = self.signer.unsign(raw_value, max_age=self.max_age).decode("utf-8")
            except BadSignature:
                logger.warning("Invalid session cookie signature - tampering or SECRET_KEY mismatch")

        data: dict[str, Any] = {}
        if session_id:
            data = await store.get(session_id) or {}
            if data and scope["type"] == "http":
                # Het schuivende venster van de rij schuift mee met activiteit.
                await store.touch(session_id)

        session = ServerSideSession(store, session_id or secrets.token_urlsafe(32), data, loop)
        scope["session"] = session
        had_data = bool(data)

        if scope["type"] == "websocket":
            # Een handshake kan geen cookie zetten of verversen; de sessie is hier
            # alleen leesmateriaal voor de endpoint zelf.
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                await session.flush()
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                if session.modified and len(session):
                    # Alleen bij mutatie een verse ondertekening: de cookie staat dan weer
                    # op "nu".
                    signed = self.signer.sign(session.session_id.encode("utf-8"))
                    header_value = "{session_cookie}={data}; path={path}; {max_age}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data=signed.decode("utf-8"),
                        path=self.path,
                        max_age=f"Max-Age={self.max_age}; " if self.max_age else "",
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
                elif session.modified and had_data:
                    # De sessie is leeggemaakt: rij weg en de cookie in de browser ook.
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
        """Statische paden en probes lezen de sessie niet: geen store-verkeer.

        Dezelfde uitsluiting als de autorisatie-middleware: wat daar geen gebruikerscontext
        heeft, heeft hier geen sessie-inhoud nodig.
        """
        path: str = scope.get("path", "")
        return path in SKIP_AUTH_EXACT or path.startswith(SKIP_AUTH_PREFIXES)
