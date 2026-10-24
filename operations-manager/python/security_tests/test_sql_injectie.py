"""SQL-injectie op de route waar een databasequery achter zit: de takenlijst.

ZAP's heuristiek (40018) valt per run op andere routes en meet daar de typecontrole tegen de
herkenning, niet een query (gemeten 24-10). Deze proef meet het wel: de takenlijst filtert in
rig-db op ``deployment_name``, en booleaanse varianten van die waarde horen hetzelfde antwoord
te geven als een naam die niet bestaat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from security_tests.conftest import sleutelclient

if TYPE_CHECKING:
    from security_tests.doel import Doel

#: Een naam die niet bestaat, en de varianten die ZAP gebruikt.
NIET_BESTAAND = "bestaat-niet"
VARIANTEN = (
    "bestaat-niet' OR '1'='1' -- ",
    "bestaat-niet' AND '1'='2' -- ",
    "bestaat-niet OR 1=1",
    "bestaat-niet; SELECT 1",
)


@pytest.mark.parametrize("variant", VARIANTEN)
def test_een_booleaanse_variant_geeft_hetzelfde_als_een_naam_die_niet_bestaat(
    doel: Doel, proefproject: str, variant: str
) -> None:
    """Faalconditie: de variant geeft andere taken, een 5xx, of taken van een ander project."""
    with sleutelclient(doel, proefproject) as client:
        basis = client.get("/api/tasks", params={"project_name": proefproject, "deployment_name": NIET_BESTAAND})
        aanval = client.get("/api/tasks", params={"project_name": proefproject, "deployment_name": variant})
    assert basis.status_code == 200, f"de basismeting faalde: {basis.status_code}"
    assert aanval.status_code == basis.status_code, f"{variant!r} gaf {aanval.status_code}"
    assert aanval.json() == basis.json(), f"{variant!r} gaf andere taken dan een naam die niet bestaat"
