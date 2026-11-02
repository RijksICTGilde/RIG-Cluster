"""Niets dat de aanroeper doet, en niets dat hier niet ingericht is, geeft een 5xx.

Een 5xx zegt "deze server is stuk". Een route die op dit cluster niet bestaat of niet is
ingericht is niet stuk: die hoort 404 te geven, en 403 als de aanroeper bekend is maar het
niet mag. De aanvalsproef vond op 24-10-2026 vier routes die 501 gaven omdat een secret
nergens stond (ZAP 100000); deze toets is de guard daarvoor (RC-386).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import opi.api.endpoint_util as endpoint_util
import pytest
from opi.api.route_herkenning import MARKERINGEN, route_herkenningen
from opi.server import app

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

#: Een ongeldige sleutel, zodat een herkenning verder komt dan "geen bewijsmiddel
#: meegestuurd" en zijn eigen controle bereikt. Geen bearertoken in de brede sweep: dat
#: laat Keycloak opzoeken en maakt de toets afhankelijk van het netwerk.
SLEUTEL = {"X-API-Key": "niet-een-geldige-sleutel"}

#: Het bewijsmiddel van de metrics-route, die een Bearer leest en geen X-API-Key.
SCRAPE_TOKEN = {"Authorization": "Bearer niet-het-scrape-token"}

#: Routes die onder deze fixture om een andere reden dan configuratie een 5xx geven, met reden.
UITGEZONDERD: dict[str, str] = {
    "/auth/login": "de OAuth-client wordt door de lifespan geregistreerd, en die draait hier niet",
}

#: De instellingen waar een herkenning op afgaat. Leeg gezet hoort elke route die ze leest
#: dicht te zijn met een 4xx, niet stuk met een 5xx.
SECRETS_VAN_EEN_HERKENNING = ("PROMETHEUS_METRICS_AUTH_TOKEN",)


def _losse_get_paden(app: object) -> list[str]:
    """Elke GET-route zonder padparameter, zodat een aanroep geen verzonnen naam nodig heeft."""
    return sorted(
        {
            route.path
            for route in app.routes  # type: ignore[attr-defined]
            if getattr(route, "endpoint", None) is not None
            and "GET" in (getattr(route, "methods", None) or set())
            and "{" not in route.path
        }
    )


@pytest.fixture
def zonder_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for naam in SECRETS_VAN_EEN_HERKENNING:
        monkeypatch.setattr(endpoint_util.settings, naam, None)


def test_geen_route_geeft_een_5xx_zonder_configuratie(test_client: TestClient, zonder_secrets: None) -> None:
    """Over de routetabel, met een ongeldige sleutel erbij."""
    stuk = []
    for pad in _losse_get_paden(test_client.app):
        if pad in UITGEZONDERD:
            continue
        response = test_client.get(pad, headers=SLEUTEL, follow_redirects=False)
        if response.status_code >= 500:
            stuk.append(f"GET {pad} geeft {response.status_code}")

    assert not stuk, "een route zonder configuratie hoort 404 of 403 te geven:\n" + "\n".join(stuk)


def test_een_niet_geconfigureerd_secret_geeft_404(test_client: TestClient, zonder_secrets: None) -> None:
    """De metrics-route is de laatste met een gedeeld secret; zonder waarde is hij dicht."""
    response = test_client.get("/metrics", headers=SCRAPE_TOKEN)

    assert response.status_code == 404
    assert "# HELP" not in response.text


def test_elke_uitzondering_bestaat_nog() -> None:
    """Een uitzondering voor een pad dat weg is verbergt een pad dat terugkomt."""
    assert "metrics-token" in MARKERINGEN
    paden = {route.pad for route in route_herkenningen(app)}
    verdwenen = [pad for pad in UITGEZONDERD if pad not in paden]

    assert not verdwenen, f"haal deze uitzonderingen weg, de routes bestaan niet meer: {verdwenen}"
