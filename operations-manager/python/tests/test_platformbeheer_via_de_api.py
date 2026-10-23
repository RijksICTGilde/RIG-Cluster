"""Platformbeheer via de API loopt op het token van een persoon, niet op een gedeelde sleutel.

Deze toets is de guard bij RC-377: hij valt om zodra een beheerroute terugkeert naar een
gedeelde sleutel, of zodra ``ADMIN_API_KEY`` weer in de code of de uitrol opduikt.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from opi.api import endpoint_util
from opi.api.route_herkenning import route_herkenningen
from opi.server import app

#: De routes die platformbeheer doen. Het pad plus de methode, zoals route_herkenningen ze geeft.
BEHEERROUTES: set[tuple[str, str]] = {
    ("GET", "/api/v2/admin/marked-for-deletion"),
    ("DELETE", "/api/v2/admin/marked-for-deletion/{mark_id}"),
    ("POST", "/api/v2/admin/cleanup/trigger"),
    ("POST", "/api/v2/admin/reconciliation/trigger"),
    ("GET", "/api/v2/admin/orphans/report"),
    ("POST", "/api/v2/admin/orphans/confirm"),
    ("POST", "/api/v2/admin/projects/:reconcile"),
    ("POST", "/api/v2/projects/{project_name}/:rotate-project-key"),
}

#: De mappen waarin de oude sleutel niet meer mag voorkomen.
GEEN_ADMIN_SLEUTEL_IN = ("operations-manager/python/opi", "bootstrap")

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("sleutel", sorted(BEHEERROUTES), ids=lambda s: f"{s[0]} {s[1]}")
def test_de_beheerroute_vraagt_het_token_van_een_platformbeheerder(sleutel: tuple[str, str]) -> None:
    gevonden = [route for route in route_herkenningen(app) if route.sleutel == sleutel]

    assert gevonden, f"{sleutel[0]} {sleutel[1]} is niet geregistreerd; is het pad veranderd?"
    for route in gevonden:
        assert route.herkenningen == ("platformbeheerder-token",), (
            f"{route} herkent {list(route.herkenningen)} in plaats van het token van een platformbeheerder"
        )


def test_de_gedeelde_adminsleutel_bestaat_niet_meer() -> None:
    """Zonder deze toets kan de decorator terugkomen zonder dat iemand het ziet."""
    assert not hasattr(endpoint_util, "validate_admin_api_key")
    assert not hasattr(endpoint_util, "REQUIRES_ADMIN_API_KEY")


@pytest.mark.parametrize("map_naam", GEEN_ADMIN_SLEUTEL_IN)
def test_admin_api_key_komt_niet_meer_voor(map_naam: str) -> None:
    """De naam zelf, in de code en in de uitrol. ``ActorKind`` leest alleen oude historie terug."""
    uitvoer = subprocess.run(
        ["git", "grep", "-n", "ADMIN_API_KEY", "--", map_naam],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout

    regels = [r for r in uitvoer.splitlines() if not re.search(r"ADMIN_API_KEY = \"admin-api-key\"", r)]

    assert not regels, "ADMIN_API_KEY komt nog voor:\n" + "\n".join(regels)
