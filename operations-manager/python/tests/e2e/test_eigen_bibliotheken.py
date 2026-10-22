"""De bibliotheken komen uit static/ en de pagina's die ze gebruiken werken nog.

Chart.js, de annotatie-plugin, Swagger-UI en ReDoc stonden op cdn.jsdelivr.net, en die
host staat niet meer in de CSP. Een verwijzing die dan blijft staan geeft geen
foutmelding, alleen een grafiek die niet getekend wordt, dus wordt hier gemeten dat er
geen verzoek meer naar een CDN gaat EN dat de bibliotheek er daadwerkelijk is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
DEPLOYMENT = "default"

EXTERNE_HOSTS = ("cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com", "fastapi.tiangolo.com")


@pytest.fixture
def externe_verzoeken(auth_page: Page) -> list[str]:
    """Elk verzoek dat de pagina naar een van de oude hosts doet."""
    naar_buiten: list[str] = []
    auth_page.on(
        "request", lambda req: naar_buiten.extend([req.url] if any(h in req.url for h in EXTERNE_HOSTS) else [])
    )
    return naar_buiten


def test_het_dashboard_heeft_chart_js_uit_static(
    auth_page: Page, app_server: str, externe_verzoeken: list[str]
) -> None:
    auth_page.goto(f"{app_server}/dashboard")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.evaluate("() => typeof window.Chart") == "function", "Chart.js is niet geladen"
    assert not externe_verzoeken, f"verzoek naar een externe host: {externe_verzoeken}"


def test_het_metingenblok_laadt_chart_js_en_zijn_plugin_bij(
    auth_page: Page, app_server: str, externe_verzoeken: list[str]
) -> None:
    """Dit fragment haalt zijn scripts zelf op met createElement('script'), dus het meet
    tegelijk of 'strict-dynamic' die weg openhoudt."""
    auth_page.goto(f"{app_server}/projects/{PROJECT}/metrics/{DEPLOYMENT}")
    # De plugin registreert zichzelf zodra hij geladen is, dus is zijn registratie het
    # signaal dat beide bestanden binnen zijn; Chart alleen is er een tel eerder.
    auth_page.wait_for_function("() => !!window.Chart?.registry.plugins.items.annotation")
    assert not externe_verzoeken, f"verzoek naar een externe host: {externe_verzoeken}"


def test_swagger_ui_laadt_uit_static(auth_page: Page, app_server: str, externe_verzoeken: list[str]) -> None:
    auth_page.goto(f"{app_server}/docs")
    auth_page.wait_for_selector(".swagger-ui .opblock")

    assert not externe_verzoeken, f"verzoek naar een externe host: {externe_verzoeken}"


def test_redoc_laadt_uit_static(auth_page: Page, app_server: str, externe_verzoeken: list[str]) -> None:
    auth_page.goto(f"{app_server}/redoc")
    auth_page.wait_for_selector("redoc h1")

    assert not externe_verzoeken, f"verzoek naar een externe host: {externe_verzoeken}"
