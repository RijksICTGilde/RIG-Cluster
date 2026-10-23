"""De wegwerp-sandbox waartegen de aanvalsproef draait.

Nagebootste binaries en test projects zodat de proeven herhaalbaar zijn.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
import yaml
from itsdangerous import TimestampSigner
from opi.utils.logging_redact import redact_invite_key_in_path
from tests.e2e.testserver import SECRET_KEY, _munt_projectsleutelpaar, create_test_app

from security_tests import sandboxlog
from security_tests.doel import AANROEPLOG, PROEF_ROOT, RUNBESTAND, TOKEN_HEADER

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: De vaste poort van de proefsandbox. Vast en niet vrij gekozen, zodat :up en :down over
#: processen heen hetzelfde ding bedoelen.
POORT = 8311

#: De proefprojecten. Twee, want de autorisatie-matrix vraagt een project waar de sleutel van
#: het andere niets mag.
PROJECTEN = ("proef-alfa", "proef-beta")

#: De rollen waarvoor het runbestand een ondertekende sessie meekrijgt.
PROJECTLID = "proef-lid@example.nl"
ZAD_ADMIN = "proef-admin@example.nl"

#: Lid van hetzelfde project met rol developer, zodat de rolpoort op de geheimen van een
#: project te beproeven is: een developer mag ze niet zien, een admin wel.
PROJECT_DEVELOPER = "proef-developer@example.nl"

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "e2e" / "fixtures" / "projects" / "test-project.yaml"


class TokenHeaderApp:
    """Zet het proeftoken op elk antwoord, zodat de grendel deze instantie kan herkennen."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_met_token(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((TOKEN_HEADER.lower().encode(), self.token.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_met_token)


#: De logger waarlangs de route van een omgevallen verzoek in het logboek komt.
_log = logging.getLogger("zad.proef")


class RouteLogApp:
    """Logt de route van elk verzoek dat omvalt of een 5xx geeft.

    Zonder deze regel staat er in het logboek een traceback zonder route: uvicorn noemt bij
    "Exception in ASGI application" niet welk verzoek het was.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pad = sandboxlog.een_regel(redact_invite_key_in_path(str(scope.get("path", ""))))
        route = f"{scope.get('method', '?')} {pad}"

        async def send_met_oordeel(message: Message) -> None:
            if message["type"] == "http.response.start" and int(message["status"]) >= 500:
                _log.error("verzoek %s gaf %s", route, message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_met_oordeel)
        except BaseException as fout:
            # logging.error en niet .exception: de traceback komt van uvicorn, en twee keer
            # dezelfde traceback maakt het logboek onleesbaar.
            _log.error("verzoek %s viel om op %s", route, type(fout).__name__)
            raise


def _zet_omgeving() -> None:
    """Nagebootste binaries voor de echte, en een leeg logboek voor hun aanroepen."""
    AANROEPLOG.parent.mkdir(parents=True, exist_ok=True)
    AANROEPLOG.write_text("")
    os.environ["ZAD_PROEF_AANROEPLOG"] = str(AANROEPLOG)
    os.environ["PATH"] = f"{PROEF_ROOT / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"


def _projectbestand(naam: str, eigenaar: str) -> dict[str, Any]:
    """Een geldig projectbestand voor een proefproject, van de e2e-fixture afgeleid."""
    data = deepcopy(yaml.safe_load(_FIXTURE.read_text()))
    data["name"] = naam
    data["display-name"] = f"Proefproject {naam}"
    data["users"] = [
        {"email": eigenaar, "role": "admin"},
        {"email": PROJECT_DEVELOPER, "role": "developer"},
    ]
    _munt_projectsleutelpaar(data)
    return data


def _seed_projecten() -> dict[str, str]:
    """Registreer de proefprojecten met een verse sleutel per project."""
    from opi.services.project_service import get_project_service
    from opi.services.user_service import get_user_service

    sleutels = {naam: secrets.token_urlsafe(24) for naam in PROJECTEN}
    projecten = get_project_service()
    for naam, sleutel in sleutels.items():
        data = _projectbestand(naam, PROJECTLID)
        projecten.register(naam, sleutel, f"{naam}.yaml", data["users"], data)
    get_user_service().add_allowed_emails([PROJECTLID, PROJECT_DEVELOPER])
    get_user_service().add_platform_admins([ZAD_ADMIN])
    return sleutels


def _sessiekoekje(email: str, naam: str) -> str:
    """Een ondertekend sessiekoekje, zoals SessionMiddleware het schrijft.

    Dezelfde opzet als ``tests/e2e/conftest.py`` en ``scripts/sandbox_project_tool.py``: er is
    geen gedeelde helper voor, en een sessie namaken is het enige dat hier van de app nodig is.
    """
    gebruiker = {"sub": email, "email": email, "name": naam, "login_at": time.time()}
    payload = base64.b64encode(json.dumps({"user": gebruiker}).encode()).decode()
    return TimestampSigner(SECRET_KEY).sign(payload).decode()


def _schrijf_runbestand(url: str, token: str, sleutels: dict[str, str]) -> None:
    RUNBESTAND.parent.mkdir(parents=True, exist_ok=True)
    RUNBESTAND.write_text(
        json.dumps(
            {
                "url": url,
                "token": token,
                "pid": os.getpid(),
                "sleutels": sleutels,
                "sessies": {
                    "projectlid": _sessiekoekje(PROJECTLID, "Proef Projectlid"),
                    "zad-admin": _sessiekoekje(ZAD_ADMIN, "Proef ZAD-admin"),
                    "project-developer": _sessiekoekje(PROJECT_DEVELOPER, "Proef Developer"),
                },
                "aanroeplog": str(AANROEPLOG),
                "gestart": time.time(),
            },
            indent=2,
        )
    )


@contextlib.contextmanager
def proefsandbox(poort: int = POORT) -> Iterator[str]:
    """Breng de proefsandbox omhoog, schrijf het runbestand, en ruim beide weer op."""
    _zet_omgeving()
    token = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{poort}"

    with create_test_app()() as app:
        sleutels = _seed_projecten()
        config = uvicorn.Config(
            RouteLogApp(TokenHeaderApp(app, token)), host="127.0.0.1", port=poort, log_level="warning"
        )
        # Na de Config, want die zet de logconfig van uvicorn (``uvicorn.error`` propageert
        # niet); eerder opvangen laat het logboek zonder ASGI-tracebacks achter.
        logboek = sandboxlog.vang_op()
        server = uvicorn.Server(config)
        draad = threading.Thread(target=server.run, daemon=True)
        draad.start()
        _wacht_op_poort(poort)
        _schrijf_runbestand(url, token, sleutels)
        try:
            yield url
        finally:
            RUNBESTAND.unlink(missing_ok=True)
            server.should_exit = True
            draad.join(timeout=10)
            sandboxlog.laat_los(logboek)


def _wacht_op_poort(poort: int, budget: float = 30.0) -> None:
    einde = time.monotonic() + budget
    while time.monotonic() < einde:
        try:
            with socket.create_connection(("127.0.0.1", poort), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"proefsandbox kwam niet omhoog op poort {poort}")


def stop(budget: float = 20.0) -> int:
    """Breek de draaiende proefsandbox af (task security-proof:down)."""
    if not RUNBESTAND.exists():
        print("geen runbestand; er draait geen proefsandbox")
        return 0
    pid = int(json.loads(RUNBESTAND.read_text())["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"proces {pid} draaide niet meer")
        RUNBESTAND.unlink(missing_ok=True)
        return 0
    einde = time.monotonic() + budget
    while time.monotonic() < einde:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"proefsandbox {pid} afgebroken")
            RUNBESTAND.unlink(missing_ok=True)
            return 0
        time.sleep(0.5)
    os.kill(pid, signal.SIGKILL)
    RUNBESTAND.unlink(missing_ok=True)
    print(f"proefsandbox {pid} geforceerd afgebroken")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Draai de sandbox op de voorgrond tot hij wordt afgebroken, of breek hem af."""
    argumenten = sys.argv[1:] if argv is None else argv
    if argumenten and argumenten[0] == "stop":
        return stop()

    # Zonder dit laat een SIGTERM het runbestand staan: het proces gaat dan dood zonder de
    # opruiming van proefsandbox() te doorlopen, en de volgende run leest een dood doel.
    signal.signal(signal.SIGTERM, _breek_af)
    with proefsandbox() as url:
        print(f"proefsandbox op {url} (runbestand: {RUNBESTAND})", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0


def _breek_af(signaalnummer: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"signaal {signaalnummer}")


if __name__ == "__main__":
    raise SystemExit(main())
