"""Een deploymentantwoord zegt waar zijn gegevens vandaan komen (zad-cli, punt 8b).

DE MELDING

``deployment describe`` noemde een URL voor een component dat nog niet was uitgerold. De
URL geeft dan 404, en aan het antwoord was niet te zien dat dat hoorde: er stond geen woord
in over gewenst tegenover werkelijk.

WAAROM DIT ANTWOORD EN NIET DE ANDERE

Elk leesantwoord van de v2-API beschrijft het PROJECTBESTAND en zegt dat ook, met ``source``
en ``pending_rollout``. Het deploymentantwoord is het enige dat twee bronnen MENGT:
``components``, ``urls`` en ``subdomain`` komen uit het projectbestand, terwijl ``status``,
``sync_revision``, ``last_synced_at`` en ``errors`` uit de cluster komen. Uitgerekend dat
antwoord droeg die twee velden niet, waardoor een lezer de hele inhoud als live las.

Een component dat met ``rollout=false`` is opgeslagen staat meteen in het projectbestand en
dus meteen in ``urls``. Dat is bedoeld gedrag; wat ontbrak is dat je het kunt ZIEN.
"""

from __future__ import annotations

from opi.api.v2.models import DeploymentDetail, DeploymentListResponse, DeploymentStatus


def _detail() -> DeploymentDetail:
    return DeploymentDetail(
        name="productie",
        project="demo",
        cluster="sandboxed-local",
        namespace="demo",
        status=DeploymentStatus.Healthy,
    )


def test_een_deployment_zegt_dat_de_beschrijving_uit_het_projectbestand_komt() -> None:
    assert _detail().source == "project-file"


def test_de_lijst_zegt_het_ook() -> None:
    """Anders hangt het antwoord op welk van de twee endpoints je toevallig aanroept."""
    lijst = DeploymentListResponse(project="demo", cluster="sandboxed-local")

    assert lijst.source == "project-file"


def test_het_drift_etiket_mag_ontbreken_maar_bestaat_wel() -> None:
    """Null betekent "hier niet gedragen", niet "er is geen drift".

    In een lijst draagt het omhullende antwoord het, want het is een eigenschap van het
    PROJECT: per deployment herhalen zou hetzelfde getal zo vaak neerzetten als er
    deployments zijn. En als de takenservice niet bereikbaar is ontbreekt het etiket,
    terwijl de beschrijving eromheen gewoon te geven is -- dat is de reden dat dit veld
    optioneel is en niet verplicht.
    """
    detail = _detail()

    assert detail.pending_rollout is None
    assert "pending_rollout" in DeploymentDetail.model_fields
    assert "pending_rollout" in DeploymentListResponse.model_fields


def test_de_beschrijving_benoemt_welk_veld_uit_welke_bron_komt() -> None:
    """De poort die het misverstand echt afsluit.

    Zeggen "project-file" is niet genoeg bij een antwoord dat MENGT: dan denkt een lezer
    dat ook de status uit het bestand komt. De beschrijving moet de scheidslijn noemen,
    anders verplaatst het misverstand zich alleen maar.
    """
    tekst = DeploymentDetail.model_fields["source"].description or ""

    assert "urls" in tekst
    assert "status" in tekst
    assert "pending_rollout" in tekst
