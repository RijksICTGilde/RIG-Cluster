"""De master-sleutel bestaat niet meer; zijn routes herkennen een platformbeheerder.

Deze toets is de guard bij RC-386, naar het model van ``test_platformbeheer_via_de_api.py``
(RC-377): hij valt om zodra ``MASTER_API_KEY`` weer in de code of de uitrol opduikt, of
zodra een van zijn oude routes terugkeert naar een gedeelde sleutel.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from opi.api import endpoint_util
from opi.api.route_herkenning import route_herkenningen
from opi.server import app

#: De routes die de master-sleutel afschermde en nu het token van een platformbeheerder vragen.
BEHEERROUTES: set[tuple[str, str]] = {
    ("GET", "/api/v1/backup/status"),
    ("GET", "/api/federation/peers"),
    ("GET", "/api/federation/health"),
}

#: De mappen waarin de naam niet meer mag voorkomen.
GEEN_MASTER_SLEUTEL_IN = ("operations-manager/python/opi", "bootstrap")

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("sleutel", sorted(BEHEERROUTES), ids=lambda s: f"{s[0]} {s[1]}")
def test_de_route_vraagt_het_token_van_een_platformbeheerder(sleutel: tuple[str, str]) -> None:
    gevonden = [route for route in route_herkenningen(app) if route.sleutel == sleutel]

    assert gevonden, f"{sleutel[0]} {sleutel[1]} is niet geregistreerd; is het pad veranderd?"
    for route in gevonden:
        assert route.herkenningen == ("platformbeheerder-token",), (
            f"{route} herkent {list(route.herkenningen)} in plaats van het token van een platformbeheerder"
        )


def test_post_api_tasks_is_niet_geregistreerd() -> None:
    """De enige aanroeper was de federatie, en een peer heeft nog geen eigen identiteit."""
    registreerd = [route for route in route_herkenningen(app) if route.sleutel == ("POST", "/api/tasks")]

    assert not registreerd, f"POST /api/tasks is terug: {registreerd}"


def test_de_gedeelde_mastersleutel_bestaat_niet_meer() -> None:
    """Zonder deze toets kan de decorator terugkomen zonder dat iemand het ziet."""
    assert not hasattr(endpoint_util, "validate_master_api_key")
    assert not hasattr(endpoint_util, "REQUIRES_MASTER_API_KEY")


@pytest.mark.parametrize("map_naam", GEEN_MASTER_SLEUTEL_IN)
def test_master_api_key_komt_niet_meer_voor(map_naam: str) -> None:
    """De naam zelf, in de code en in de uitrol. ``ActorKind`` leest alleen oude historie terug."""
    uitvoer = subprocess.run(
        ["git", "grep", "-n", "-E", "MASTER_API_KEY|validate_master_api_key", "--", map_naam],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout

    regels = [r for r in uitvoer.splitlines() if "KEYCLOAK_MASTER_" not in r]

    assert not regels, "MASTER_API_KEY komt nog voor:\n" + "\n".join(regels)
