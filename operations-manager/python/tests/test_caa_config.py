"""De CAA-grendel mag nooit een domein buitensluiten dat wij zelf uitgeven.

Zonder CAA mag elke publiek vertrouwde CA een certificaat uitgeven voor elke naam onder
onze zones. Met CAA staat in DNS wie dat mag -- en precies daar zit het risico van dit
ship: niet in het zetten, maar in het later toevoegen van een dienst onder onze zone die
bij een andere CA vernieuwt. Die vernieuwing faalt dan stil, negentig dagen later.

Daarom loopt de tweede test hieronder alle clusters langs: elke ``supported_domains``-entry
met een ``issuer`` die onder een beheerde zone valt, moet een issuer noemen die die zone
toestaat. De fout wordt zo een rode test op het moment dat het domein wordt toegevoegd.
"""

from __future__ import annotations

import pytest
from opi.core.cluster_config import CLUSTER_CONFIG
from opi.core.dns_config import MANAGED_DNS_ZONES, desired_caa_contents


def test_desired_contents_per_zone() -> None:
    """Elke beheerde zone krijgt issue plus issuewild voor Let's Encrypt."""
    for zone in MANAGED_DNS_ZONES:
        assert desired_caa_contents(zone) == [
            '0 issue "letsencrypt.org"',
            '0 issuewild "letsencrypt.org"',
        ], f"onverwachte CAA-inhoud voor {zone}"


def test_unmanaged_zone_is_refused() -> None:
    """Een zone die we niet beheren heeft geen gewenste inhoud."""
    with pytest.raises(ValueError, match="not a managed DNS zone"):
        desired_caa_contents("example.org")


def _managing_zone(domain: str) -> str | None:
    """De beheerde zone waar dit domein onder valt, of None."""
    for zone in MANAGED_DNS_ZONES:
        if domain == zone or domain.endswith(f".{zone}"):
            return zone
    return None


def test_every_nice_url_domain_under_managed_zone_uses_allowed_issuer() -> None:
    """Geen enkel cluster geeft uit onder onze zones met een CA die CAA niet toestaat."""
    checked = 0
    for cluster_name, cluster in CLUSTER_CONFIG.items():
        for entry in cluster.get("nice_url", {}).get("supported_domains", []):
            issuer = entry.get("issuer")
            if not issuer:
                # Geen issuer betekent: hier wordt niets uitgegeven (kind, local,
                # sandbox.rijksapp.dev draait op een vooraf geplaatste wildcard).
                continue
            zone = _managing_zone(entry["domain"])
            if zone is None:
                # Buiten onze zones bepaalt de eigenaar van dat domein zijn eigen CAA.
                continue
            checked += 1
            assert issuer in MANAGED_DNS_ZONES[zone], (
                f"cluster {cluster_name}: {entry['domain']} geeft uit via '{issuer}', "
                f"maar de CAA-records op {zone} staan alleen {MANAGED_DNS_ZONES[zone]} toe"
            )

    assert checked > 0, "geen enkel domein onder een beheerde zone gecontroleerd"
