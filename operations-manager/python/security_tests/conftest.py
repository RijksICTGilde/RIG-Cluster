"""Fixtures van de aanvalsproef: het geverifieerde doel en de drie aanroepers.

Elke proef praat via deze fixtures met de sandbox, zodat de grendel uit ``doel.py`` op geen
enkele weg te omzeilen is.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import pytest

from security_tests.doel import Doel, OnveiligDoel, huidig_doel
from security_tests.sandboxlog import markeer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Ruim voor een lokale instantie, kort genoeg om een hangend verzoek als hangend te zien.
TIJDBUDGET = 15.0

#: Het ``Secure``-attribuut van een koekje, met de puntkomma ervoor.
_SECURE = re.compile(r";\s*secure", re.IGNORECASE)

#: Hoe lang ``ps`` mag doen over een geheugenbeeld.
_PS_TIJDBUDGET = 10.0


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """Bakent het venster van deze proef af in het logboek van de sandbox."""
    markeer(nodeid, "start")


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int | None, str]) -> None:
    markeer(nodeid, "einde")


class GeenGeheugenmeting(Exception):
    """Het geheugenbeeld van een proces is op dit platform niet op te vragen."""


def rss_bytes(pid: int) -> int:
    """Het residente geheugen van een proces, in bytes.
    Via ``ps`` in plaats van ``/proc``, want macOS heeft geen ``/proc``."""
    try:
        uitkomst = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIJDBUDGET,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as fout:
        raise GeenGeheugenmeting(f"ps is hier niet te draaien: {fout}") from fout
    waarde = uitkomst.stdout.strip()
    if uitkomst.returncode != 0 or not waarde.isdigit():
        raise GeenGeheugenmeting(f"ps gaf geen rss voor pid {pid} (exit {uitkomst.returncode}, uitvoer {waarde!r})")
    return int(waarde) * 1024


def leeft(pid: int) -> bool:
    """Of dit proces er nog is. Signaal 0 levert geen signaal af, alleen de controle."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ZonderSecure(httpx.BaseTransport):
    """Haalt ``Secure`` van sessiekoekjes: de sandbox dient http, maar OPI zet ``Secure``."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        antwoord = self._transport.handle_request(request)
        koekjes = antwoord.headers.get_list("set-cookie")
        if koekjes:
            rest = [(naam, waarde) for naam, waarde in antwoord.headers.multi_items() if naam.lower() != "set-cookie"]
            antwoord.headers = httpx.Headers(rest + [("set-cookie", _SECURE.sub("", k)) for k in koekjes])
        return antwoord


def proefclient(doel: Doel, *, timeout: float = TIJDBUDGET, **opties: Any) -> httpx.Client:
    """Een aanroeper van de proefsandbox. De enige plek waar een client wordt gemaakt."""
    return httpx.Client(
        base_url=doel.url,
        timeout=timeout,
        follow_redirects=False,
        transport=ZonderSecure(),
        **opties,
    )


@pytest.fixture(scope="session")
def doel() -> Doel:
    """De zelf opgezette proefsandbox, of een skip als er geen draait."""
    try:
        return huidig_doel()
    except OnveiligDoel as fout:
        pytest.skip(f"geen proefsandbox: {fout}")


@pytest.fixture(scope="session")
def proefproject(doel: Doel) -> str:
    """Het eerste proefproject met een API-sleutel, of skip als dit doel er geen heeft."""
    if not doel.projecten:
        pytest.skip("dit doel heeft geen proefproject met een API-sleutel (ZAD_PROEF_SLEUTELS)")
    return doel.projecten[0]


@pytest.fixture
def anoniem(doel: Doel) -> Iterator[httpx.Client]:
    """Een aanroeper zonder enige herkenning."""
    with proefclient(doel) as client:
        yield client


@pytest.fixture
def projectlid(doel: Doel) -> Iterator[httpx.Client]:
    """Een ingelogd projectlid van proef-alfa, zonder platformrechten."""
    with _sessieclient(doel, "projectlid") as client:
        yield client


@pytest.fixture
def project_developer(doel: Doel) -> Iterator[httpx.Client]:
    """Een ingelogd lid van hetzelfde project met rol developer, dus zonder recht op de geheimen."""
    with _sessieclient(doel, "project-developer") as client:
        yield client


@pytest.fixture
def zad_admin(doel: Doel) -> Iterator[httpx.Client]:
    """Een ingelogde ZAD-admin."""
    with _sessieclient(doel, "zad-admin") as client:
        yield client


def _sessieclient(doel: Doel, rol: str) -> httpx.Client:
    """Een aanroeper met de ondertekende sessie van een rol, of een skip als die er niet is."""
    if rol not in doel.sessies:
        pytest.skip(f"dit doel heeft geen ondertekende sessie voor rol {rol}")
    client = proefclient(doel)
    client.cookies.set("session", doel.sessies[rol], domain=urlparse(doel.url).hostname or "")
    return client


def sleutelclient(doel: Doel, project: str) -> httpx.Client:
    """Een aanroeper met de projectsleutel van ``project``."""
    return proefclient(doel, headers={"X-API-Key": doel.sleutels[project]})


def met_csrf(client: httpx.Client) -> dict[str, str]:
    """Haal een CSRF-token op en geef de headers waarmee een formulierpost geldig is."""
    client.get("/")
    token = client.cookies.get("csrf_token") or ""
    return {"X-CSRF-Token": token, "Origin": str(client.base_url), "Referer": f"{client.base_url}/"}


def aanroepen(log: Path) -> list[dict]:
    """De aanroepen die de nagebootste binaries hebben weggeschreven."""
    if not log.exists():
        return []
    return [json.loads(regel) for regel in log.read_text().splitlines() if regel.strip()]
