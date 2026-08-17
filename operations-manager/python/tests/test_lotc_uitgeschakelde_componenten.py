"""Uitgeschakelde componenten: EEN melding per oorzaak, niet een per component.

Op de statuskaart stond een foutblok per uitgeschakeld component, met per stuk vier
regels uitleg en een uitklapper "Technische details". Bij zes componenten is dat zes keer
dezelfde tekst: een lange rode brij waarin het enige wat verschilt - welk component, en
welke image - ondersneeuwt.

Wat gelijk is hoort een keer (de kop en het advies), wat verschilt hoort in de lijst.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc

SJABLOON = "bg/_argocd-deployment-card.html.j2"


def _component(naam: str, reden: str, image: str | None = None) -> dict[str, Any]:
    return {"reference": naam, "disabled": True, "disabled-reason": reden, "image": image}


def _render(componenten: list[dict[str, Any]]) -> str:
    deployment = {"name": "dep", "cluster": "sandboxed-local", "components": componenten}
    return templates_lotc.env.get_template(SJABLOON).render(
        deployment=deployment,
        project={"name": "proj"},
        current_cluster="sandboxed-local",
        argocd_status={},
        deployment_states={},
    )


IMAGE_REDEN = "ImagePullBackOff: manifest for ghcr.io/x/y:1 not found, unknown tag"


def test_zes_componenten_leveren_een_blok() -> None:
    """De melding die aanleiding was: zes keer hetzelfde blok onder elkaar."""
    html = _render([_component(f"comp-{i}", IMAGE_REDEN, f"ghcr.io/x/y:{i}") for i in range(6)])
    assert html.count("<nldd-banner") == 1, "er hoort een foutblok te staan, niet een per component"
    for i in range(6):
        assert f"comp-{i}" in html, "elk component hoort in de lijst genoemd te worden"
        assert f"ghcr.io/x/y:{i}" in html, "de image is wat per component verschilt"


def test_de_gedeelde_oorzaak_staat_in_de_kop() -> None:
    html = _render([_component("a", IMAGE_REDEN, "i:1"), _component("b", IMAGE_REDEN, "i:2")])
    assert "2 componenten zijn uitgeschakeld: image ontbreekt" in html


def test_enkelvoud_bij_een_component() -> None:
    """'1 componenten' bestaat niet."""
    html = _render([_component("a", IMAGE_REDEN, "i:1")])
    assert "1 component is uitgeschakeld: image ontbreekt" in html
    assert "componenten zijn" not in html


def test_het_advies_staat_er_een_keer() -> None:
    """Wat gelijk is hoort een keer; dat was de klacht."""
    html = _render([_component(f"c{i}", IMAGE_REDEN, f"i:{i}") for i in range(4)])
    assert html.count("Push een geldige image") == 1


def test_geen_uitklapper_met_technische_details() -> None:
    """Vier alinea's plus een uitklapper per component was te veel voor deze kaart."""
    html = _render([_component("a", IMAGE_REDEN, "i:1")])
    assert "Technische details" not in html
    # de ruwe dump hoort niet in de MELDING; hij staat verderop nog wel in de
    # logs-knop, die de componenten als gegeven meekrijgt
    melding = html.partition("nldd-banner")[2].partition("</nldd-banner>")[0]
    assert "ImagePullBackOff" not in melding


def test_een_andere_oorzaak_krijgt_een_eigen_blok_met_de_reden() -> None:
    """Daar is de reden juist WEL het verschil per component, dus die staat erbij."""
    html = _render([_component("a", IMAGE_REDEN, "i:1"), _component("b", "OOMKilled na 3 herstarts")])
    assert html.count("<nldd-banner") == 2
    assert "1 component is uitgeschakeld" in html
    assert "OOMKilled na 3 herstarts" in html


def test_een_lange_reden_wordt_afgekapt() -> None:
    """Een dump van duizend tekens maakt de kaart weer even lang als eerst."""
    html = _render([_component("a", "x" * 400)])
    assert "x" * 120 in html
    assert "x" * 130 not in html


@pytest.mark.parametrize("reden", ["ImagePullBackOff: ...", "InvalidImageName", "manifest unknown"])
def test_de_image_oorzaken_landen_in_het_image_blok(reden: str) -> None:
    html = _render([_component("a", reden, "i:1")])
    assert "image ontbreekt" in html
