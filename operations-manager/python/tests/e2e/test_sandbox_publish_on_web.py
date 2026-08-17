"""Live sandbox E2E: publish-on-web is via alle drie de API-wegen aan te zetten (RC-103).

De regressie uit vraag 12 van ``plans/vragen-uit-zad-cli.md``: de poort voor impliciete
dienstselectie weigerde ``publish-on-web`` met "eerst op projectniveau aanzetten", terwijl
de dienst geen projectlaag draagt - de catalogus meldt hem niet en er is geen endpoint voor.
Drie routes, dezelfde muur, en het draaiboek van de zad-cli stond stil.

``tests/test_implicit_project_selection.py`` legt de beslissing vast op de haak zelf. Wat
daar niet in zit is de weg die de aanroeper werkelijk loopt: een echte API-sleutel, een
echte taak, en het projectbestand in Forgejo als toets van record. Elke route krijgt zijn
EIGEN verse project, want zodra de eerste route de dienst op projectniveau heeft gezet
loopt de tweede niet meer door de poort die hier gemeten wordt.

Slaat over zonder E2E_BASE_URL.

Draaien:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    uv run pytest tests/e2e/test_sandbox_publish_on_web.py -m "e2e and sandbox" -q -o addopts=""
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

import httpx
import pytest
from opi.services import ServiceType
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import get_service
from opi.services.services import service_entry_name
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, create_project_with_services

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")

#: De dienst uit de regressie. Zonder projectlaag, dus een kale selectie hoort te lukken.
DIENST = ServiceType.PUBLISH_ON_WEB.value


def _project_diensten(forgejo: ForgejoClient, project_name: str) -> list[str]:
    """De namen op de projectlijst ``services``, uit het bestand in Forgejo."""
    data = forgejo.get_project_yaml(project_name) or {}
    return [service_entry_name(entry) for entry in (data.get("services") or [])]


@pytest.fixture
def vers_project(
    sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient
) -> Generator[Callable[[str], tuple[str, str]]]:
    """Maak een project ZONDER publish-on-web; ruimt op na de test.

    Zonder de dienst is de poort de eerste die de route tegenkomt - dat is precies wat
    hier gemeten wordt.
    """
    gemaakt: list[tuple[str, str]] = []

    def maak(naam: str) -> tuple[str, str]:
        page = sandbox_context.new_page()
        try:
            project = create_project_with_services(
                page, sandbox_url, forgejo, naam, user_email=_USER_EMAIL, services=[], create_timeout=600.0
            )
        finally:
            page.close()
        gemaakt.append((project.name, project.api_key))
        return project.name, project.api_key

    try:
        yield maak
    finally:
        for naam, sleutel in gemaakt:
            with contextlib.suppress(Exception):
                sandbox_api.delete_project_via_api(sandbox_url, naam, sleutel, verify_ssl=_API_VERIFY_SSL, timeout=600)


def test_de_dienst_draagt_echt_geen_projectlaag() -> None:
    """De premisse van deze module: krijgt publish-on-web ooit een projectlaag, dan is een
    weigering weer terecht en meet de rest van dit bestand iets anders dan het beweert."""
    assert ConfigLayer.PROJECT not in get_service(ServiceType.PUBLISH_ON_WEB).config_layers()


def test_componentconfig_zet_de_dienst_aan(
    sandbox_url: str, forgejo: ForgejoClient, vers_project: Callable[[str], tuple[str, str]]
) -> None:
    """Route 1: ``PUT /v2/projects/{p}/services/publish-on-web/config/component/{naam}``."""
    naam, sleutel = vers_project("rc103-config")

    taak = sandbox_api.start_task(
        sandbox_url,
        "PUT",
        f"/api/v2/projects/{naam}/services/{DIENST}/config/component/web?rollout=false",
        sleutel,
        {"tls": "standard"},
        verify_ssl=_API_VERIFY_SSL,
    )
    sandbox_api.wait_for_task(sandbox_url, taak, sleutel, verify_ssl=_API_VERIFY_SSL, timeout=600)

    assert DIENST in _project_diensten(forgejo, naam)


def test_component_bijwerken_zet_de_dienst_aan(
    sandbox_url: str, forgejo: ForgejoClient, vers_project: Callable[[str], tuple[str, str]]
) -> None:
    """Route 2: ``PATCH /v2/projects/{p}/components/{naam}`` met de dienst in de lijst."""
    naam, sleutel = vers_project("rc103-patch")

    taak = sandbox_api.start_task(
        sandbox_url,
        "PATCH",
        f"/api/v2/projects/{naam}/components/web?rollout=false",
        sleutel,
        {"services": [DIENST]},
        verify_ssl=_API_VERIFY_SSL,
    )
    sandbox_api.wait_for_task(sandbox_url, taak, sleutel, verify_ssl=_API_VERIFY_SSL, timeout=600)

    assert DIENST in _project_diensten(forgejo, naam)


def test_component_toevoegen_zet_de_dienst_aan(
    sandbox_url: str, forgejo: ForgejoClient, vers_project: Callable[[str], tuple[str, str]]
) -> None:
    """Route 3: ``POST /v2/projects/{p}/components`` met de dienst er meteen bij."""
    naam, sleutel = vers_project("rc103-post")

    with httpx.Client(verify=_API_VERIFY_SSL, timeout=30.0) as client:
        response = client.post(
            f"{sandbox_url.rstrip('/')}/api/v2/projects/{naam}/components",
            json={"name": "web2", "image": RUNNABLE_IMAGE, "deployment_names": [], "services": [DIENST]},
            headers={"X-API-Key": sleutel, "Content-Type": "application/json"},
        )
    assert response.status_code == 202, f"{response.status_code}: {response.text}"
    taak = (response.headers.get("Location") or "").rsplit("/", 1)[-1] or response.json().get("task_id")
    sandbox_api.wait_for_task(sandbox_url, taak, sleutel, verify_ssl=_API_VERIFY_SSL, timeout=600)

    assert DIENST in _project_diensten(forgejo, naam)
