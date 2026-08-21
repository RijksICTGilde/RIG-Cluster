"""Schermafdrukken van /admin/approvals: een dienstaanvraag naast domeinaanvragen.

Geen assertie op een pixel -- dit legt het BEELD vast waarop de wijziging beoordeeld
wordt. Twee projecten in een run, zodat het verschil tussen "Publiceren op het web" met
zijn domein- en subdomeinregels en "E-mail versturen" met zijn ene regel op een afdruk
naast elkaar staat.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

MAP = Path(__file__).parent / "screenshots" / "lotc"

PROJECTEN: dict[str, dict[str, Any]] = {
    "aanvragen-mail": {
        "name": "aanvragen-mail",
        "config": {"api-key": "aanvragen-mail-key"},
        "services": [
            {
                "name": "send-email",
                "config": {
                    "from-name": "Robbert Uittenbroek",
                    "messages-per-day": 100,
                    "approval": {"status": "requested", "history": []},
                },
            }
        ],
    },
    "aanvragen-web": {
        "name": "aanvragen-web",
        "config": {"api-key": "aanvragen-web-key"},
        "domains": {
            "allowed-domains": [
                {
                    "domain": "voorbeeld.nl",
                    "status": "requested",
                    "supports-dots": False,
                    "history": [{"date": "2026-08-16T10:00:00+00:00", "status": "requested"}],
                }
            ],
            "allowed-subdomains": [
                {
                    "domain": "sandbox.rijksapp.dev",
                    "subdomains": [
                        {
                            "name": "mijnapp",
                            "status": "approved",
                            "history": [
                                {
                                    "date": "2026-08-16T10:00:00+00:00",
                                    "status": "approved",
                                    "by": "admin@sandbox.rijksapp.dev",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    },
}


@pytest.fixture
def projecten_met_aanvragen(app_server: str) -> Iterator[None]:
    """Zet beide projecten in de draaiende testserver, en haal ze daarna weg.

    Weghalen is geen nettigheid maar noodzaak: tests/e2e/test_lotc_projecten.py toetst de
    projectenlijst op zijn geheel, en een blijvertje maakt die test stuk.
    """
    from opi.services.project_service import get_project_service

    dienst = get_project_service()
    for naam, data in PROJECTEN.items():
        dienst.register(naam, f"{naam}-key", f"{naam}.yaml", [], data)
    try:
        yield
    finally:
        for naam in PROJECTEN:
            dienst.remove_project(naam)


def test_de_aanvragenpagina_op_beeld(app_server: str, auth_page: Page, projecten_met_aanvragen: None) -> None:
    """De hele pagina, met beide projecten."""
    auth_page.set_viewport_size({"width": 1440, "height": 1000})
    auth_page.goto(f"{app_server}/admin/approvals")
    auth_page.wait_for_selector("nldd-table", timeout=15000)
    auth_page.wait_for_function("() => !document.querySelector('*:not(:defined)')", timeout=15000)

    MAP.mkdir(parents=True, exist_ok=True)
    auth_page.screenshot(path=str(MAP / "aanvragen-per-dienst.png"), full_page=True)

    tekst = auth_page.locator("#approvals-gebied").inner_text()
    assert "E-mail versturen" in tekst, tekst
    assert "Publiceren op het web" in tekst, tekst
