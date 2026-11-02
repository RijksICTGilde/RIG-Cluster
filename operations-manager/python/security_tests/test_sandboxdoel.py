"""De proeven die alleen tegen een sandbox in het cluster iets meten.

De wegwerp-sandbox op de eigen machine heeft geen ingress, geen Keycloak, geen object storage
en geen registry, dus wat hier staat slaat zich daar over met de reden. Tegen een geclaimde
sandbox is het wel te meten, en dan langs de ingress zoals een gebruiker binnenkomt.

De grendel uit ``doel.py`` geldt ook hier: elk dienstadres wordt op het sandboxdomein
samengesteld en gaat door ``verifieer_host`` voordat er een verzoek naartoe gaat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from security_tests.conftest import proefclient
from security_tests.doel import SANDBOX, SANDBOXDOMEIN, soort_doel, verifieer_host
from security_tests.test_stortvloed import DREMPEL, TIJDBUDGET_POGING, doe_pogingen

if TYPE_CHECKING:
    from collections.abc import Iterator

    from security_tests.doel import Doel

#: Statussen waarmee een dienst een vreemde mag wegsturen. Een 2xx met inhoud is de bevinding.
GEWEIGERD = frozenset({400, 401, 403, 404, 405})


@pytest.fixture(autouse=True)
def alleen_tegen_een_sandbox(doel: Doel) -> None:
    """Deze proeven meten de echte keten, dus ze vragen een sandbox in het cluster."""
    if doel.soort != SANDBOX:
        pytest.skip("alleen tegen een geclaimde sandbox te meten (task security-proof:sandbox)")


def dienstadres(naam: str) -> str:
    """Het adres van een dienst op het sandboxdomein, door dezelfde grendel als het doel zelf.

    Op naam samengesteld en niet uit de host van het doel afgeleid: dat gaf bij een doel op
    ``sandbox.rijksapp.dev`` zelf het ouderdomein, en dus verzoeken naar een host die de
    grendel afwijst.
    """
    adres = f"https://{naam}.{SANDBOXDOMEIN}"
    verifieer_host(adres)
    return adres


@pytest.fixture
def dienstclient(doel: Doel) -> Iterator[httpx.Client]:
    """Een aanroeper zonder herkenning voor de andere diensten op het sandboxdomein."""
    with httpx.Client(timeout=TIJDBUDGET_POGING, follow_redirects=False, verify=False) as client:  # noqa: S501
        # verify=False: de sandbox gebruikt een eigen uitgever, en een certificaatfout zou de
        # proef op de opstelling laten stranden in plaats van op de dienst.
        yield client


def test_een_vervalste_x_forwarded_for_verschuift_de_limiter_niet(doel: Doel) -> None:
    """Faalconditie: de limiter telt op het IP uit de header, dus elke poging is een nieuwe client.

    Achter de ingress staat ``TRUSTED_PROXY_HOPS=1``, dus ``client_ip`` neemt de waarde die de
    ingress zelf toevoegde en niet de waarde die de aanroeper erbij zette. Als dat omgekeerd
    was, zou een aanvaller de rem van RC-368 omzeilen door per poging een ander IP te verzinnen.
    """
    pad = "/api/v2/projects/proef-bestaat-niet/deployments"
    pogingen = []
    for nummer in range(DREMPEL + 5):
        headers = {"X-API-Key": "fout", "X-Forwarded-For": f"203.0.113.{nummer % 250}"}
        with proefclient(doel, timeout=TIJDBUDGET_POGING, headers=headers) as client:
            pogingen += doe_pogingen(client, pad, 1)
    statussen = [poging.status for poging in pogingen]
    assert all(status in (401, 403, 404, 429) for status in statussen), f"statussen: {statussen}"
    assert 429 in statussen, (
        "geen enkele poging werd geremd terwijl elke poging een ander X-Forwarded-For meegaf;"
        " de limiter telt dan op het vervalste IP"
    )


def test_keycloak_geeft_geen_realmbeheer_aan_een_vreemde(dienstclient: httpx.Client) -> None:
    """Faalconditie: de realmlijst van Keycloak is zonder herkenning op te halen."""
    antwoord = dienstclient.get(f"{dienstadres('keycloak')}/admin/realms")
    assert antwoord.status_code in GEWEIGERD, f"Keycloak gaf {antwoord.status_code} op /admin/realms"


def test_de_registry_weigert_een_anonieme_catalogus(dienstclient: httpx.Client) -> None:
    """Faalconditie: de catalogus van de registry is zonder herkenning te lezen."""
    antwoord = dienstclient.get(f"{dienstadres('registry')}/v2/_catalog")
    assert antwoord.status_code in GEWEIGERD, f"de registry gaf {antwoord.status_code} op /v2/_catalog"


def test_de_object_storage_weigert_een_vreemde(dienstclient: httpx.Client) -> None:
    """Faalconditie: de S3-poort geeft een vreemde een bucketlijst of de inhoud van een bucket."""
    basis = dienstadres("minio")
    bevindingen = []
    for pad in ("/", "/zad-deployments", "/zad-projects"):
        antwoord = dienstclient.get(f"{basis}{pad}", headers={"Authorization": "AWS4-HMAC-SHA256 Credential=vreemde"})
        if antwoord.status_code not in GEWEIGERD:
            bevindingen.append(f"GET {pad} gaf {antwoord.status_code}")
    assert not bevindingen, "\n".join(bevindingen)


def test_argocd_geeft_geen_applicaties_aan_een_vreemde(dienstclient: httpx.Client) -> None:
    """Faalconditie: de applicatielijst van ArgoCD is zonder token op te halen.

    Een tenant hoort ArgoCD niet te kunnen lezen: daar staan de applicaties van elk project.
    """
    antwoord = dienstclient.get(f"{dienstadres('argo')}/api/v1/applications")
    assert antwoord.status_code in GEWEIGERD, f"ArgoCD gaf {antwoord.status_code} op /api/v1/applications"


def test_de_netpolmatrix_wordt_niet_vanuit_dit_harnas_gemeten() -> None:
    """NIET GEMETEN, bewust: hiervoor is een pod in een tenant-namespace nodig.

    Of een tenantpod rig-db, de Keycloak-admin en de gRPC-poorten van SeaweedFS niet bereikt,
    is van binnen het cluster te meten en niet langs de ingress. Daar is ``task netpol:meet``
    voor, dat zijn eigen proefpods start; dit harnas praat alleen met de buitenkant.
    """
    pytest.skip("netpolmatrix: te meten met task netpol:meet, niet langs de ingress")


class TestDienstadres:
    """De grendel op het dienstadres zelf, dus zonder een draaiende sandbox."""

    @pytest.fixture(autouse=True)
    def alleen_tegen_een_sandbox(self) -> None:
        """Overschrijft de skip van de module: dit is een toets op het adres, niet op een dienst."""

    def test_elk_dienstadres_komt_door_de_grendel(self) -> None:
        """Faalconditie: het adres ligt buiten het sandboxdomein, en dan gaat er een verzoek naar
        een host die de grendel zelf afwijst (eerder gebeurd bij een doel op het domein zelf)."""
        for naam in ("keycloak", "registry", "minio", "argo"):
            adres = dienstadres(naam)
            assert adres == f"https://{naam}.{SANDBOXDOMEIN}"
            assert soort_doel(adres) == SANDBOX
