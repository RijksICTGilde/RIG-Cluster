"""Het metingenfragment wordt werkelijk OPGEVRAAGD, niet alleen aangekondigd.

Dit bestaat om een gemeten reden. Het tabblad Metrics zet een leeg blok neer dat zijn
inhoud pas ophaalt zodra het in beeld komt (``hx-trigger="intersect once"``), en de
browsertest op dat tabblad toetst of dat blok er STAAT. Toen het fragment zelf 500 gaf op
een ``NameError`` bleef die test dus groen, bleef de hele suite groen, en meldde ook
pyright niets: geen enkele test vroeg het adres op.

Een HTTP-test die het adres wel opvraagt is de goedkoopste vangrail die er is - alles wat
de route bij het renderen aanraakt komt langs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.conftest import TEST_USER, _sign_session

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
DEPLOYMENT = "default"


@pytest.fixture
def client(app_server: str) -> Iterator[httpx.Client]:
    cookie = _sign_session({"user": TEST_USER})
    with httpx.Client(base_url=app_server, cookies={"session": cookie}, follow_redirects=True, timeout=60) as c:
        yield c


def test_het_fragment_geeft_html_en_geen_500(client: httpx.Client) -> None:
    antwoord = client.get(f"/projects/details/{PROJECT}/metrics/{DEPLOYMENT}")

    assert antwoord.status_code == 200, f"het metingenfragment gaf {antwoord.status_code}"
    # Het blok rendert zijn eigen tijdvakkeuze, ook zonder metingen.
    assert "metrics" in antwoord.text.lower()


@pytest.mark.parametrize("duration", [60, 1440, 999])
def test_elk_tijdvak_rendert_ook_een_onbekend(client: httpx.Client, duration: int) -> None:
    """Een onbekend tijdvak valt terug op het kleinste in plaats van te struikelen."""
    antwoord = client.get(f"/projects/details/{PROJECT}/metrics/{DEPLOYMENT}?duration={duration}")

    assert antwoord.status_code == 200, f"duration={duration} gaf {antwoord.status_code}"


def test_een_onbekende_deployment_is_een_404_en_geen_500(client: httpx.Client) -> None:
    antwoord = client.get(f"/projects/details/{PROJECT}/metrics/bestaat-niet")

    assert antwoord.status_code == 404, f"onbekende deployment gaf {antwoord.status_code}"
