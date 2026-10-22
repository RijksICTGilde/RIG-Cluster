"""De ASVS-meetlat: elke eis op het gekozen niveau heeft een oordeel dat te volgen is.

Zonder deze toets zegt de afvinklijst niets: een eis kan ongemerkt uit de lijst vallen, en
een regel kan naar een toets wijzen die niet meer bestaat. Zie features/asvs-zelfbeoordeling.md.
"""

from typing import TYPE_CHECKING

import pytest
import yaml
from opi.tools.asvs_render import (
    AFVINKLIJST,
    BEOORDELING,
    EISEN,
    Oordeel,
    Status,
    laad_beoordeling,
    laad_eisen,
    main,
    meldingen,
    render,
    toets_bestaat,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_elke_eis_heeft_een_oordeel_en_elke_genoemde_toets_bestaat() -> None:
    eisen = laad_eisen()
    _, oordelen = laad_beoordeling()
    assert not meldingen(eisen, oordelen)


def test_de_afvinklijst_loopt_niet_achter() -> None:
    """Het document is gegenereerd; achterlopen betekent dat iemand het met de hand bewerkte."""
    eisen = laad_eisen()
    niveau, oordelen = laad_beoordeling()
    assert AFVINKLIJST.read_text(encoding="utf-8") == render(eisen, niveau, oordelen), (
        "docs/asvs-zelfbeoordeling.md loopt achter, genereer opnieuw met `task asvs-afvinklijst`"
    )


def test_de_eisen_komen_uit_de_standaard_en_dekken_l1_en_l2() -> None:
    inhoud = yaml.safe_load(EISEN.read_text(encoding="utf-8"))
    assert inhoud["asvs-versie"] == "5.0.0"
    niveaus = {eis["niveau"] for eis in inhoud["eisen"]}
    assert niveaus == {1, 2}, f"niveaus in de meetlat: {niveaus}"


def test_het_gekozen_niveau_staat_in_de_beoordeling() -> None:
    niveau, _ = laad_beoordeling()
    assert niveau == "L2"


@pytest.mark.parametrize(
    ("oordeel", "verwacht"),
    [
        (Oordeel(id="V1.1.1", status=Status.TOETS), "status toets zonder een genoemde toets"),
        (
            Oordeel(id="V1.1.1", status=Status.TOETS, toets="tests/bestaat-niet.py"),
            "genoemde toets tests/bestaat-niet.py bestaat niet",
        ),
        (Oordeel(id="V1.1.1", status=Status.PLAN), "status plan zonder een genoemd plan"),
        (Oordeel(id="V1.1.1", status=Status.HANDWERK), "status handwerk zonder eigenaar"),
        (Oordeel(id="V1.1.1", status=Status.NVT), "status nvt zonder toelichting"),
    ],
)
def test_een_oordeel_zonder_onderbouwing_is_een_gat(oordeel: Oordeel, verwacht: str) -> None:
    eisen = [eis for eis in laad_eisen() if eis.id == "V1.1.1"]
    gevonden = meldingen(eisen, [oordeel])
    assert any(verwacht in melding for melding in gevonden), gevonden


def test_een_eis_zonder_oordeel_en_een_oordeel_zonder_eis_vallen_op() -> None:
    eisen = [eis for eis in laad_eisen() if eis.id in ("V1.1.1", "V1.1.2")]
    gevonden = meldingen(eisen, [Oordeel(id="V9.9.9", status=Status.NVT, toelichting="verzonnen")])
    assert any("V1.1.1: geen oordeel" in m for m in gevonden), gevonden
    assert any("V9.9.9" in m and "niet op dit niveau" in m for m in gevonden), gevonden


def test_twee_oordelen_over_dezelfde_eis_vallen_op() -> None:
    eisen = [eis for eis in laad_eisen() if eis.id == "V1.1.1"]
    twee = [
        Oordeel(id="V1.1.1", status=Status.NVT, toelichting="een"),
        Oordeel(id="V1.1.1", status=Status.NVT, toelichting="twee"),
    ]
    assert any("2 oordelen" in m for m in meldingen(eisen, twee))


def test_een_verwijzing_naar_een_job_toetst_ook_de_job(tmp_path: Path) -> None:
    workflow = tmp_path / "wf.yml"
    workflow.write_text(yaml.safe_dump({"jobs": {"bestaat": {}}}), encoding="utf-8")
    assert toets_bestaat("wf.yml#bestaat", tmp_path)
    assert not toets_bestaat("wf.yml#bestaat-niet", tmp_path)
    assert not toets_bestaat("ander.yml", tmp_path)


def _schrijf_bronnen(bronmap: Path, oordelen: list[dict[str, str]]) -> None:
    """Een meetlat van een eis en een beoordeling ernaast, voor de CLI-paden."""
    (bronmap / "asvs-5.0-eisen.yaml").write_text(
        yaml.safe_dump(
            {
                "asvs-versie": "5.0.0",
                "eisen": [
                    {"id": "V1.1.1", "niveau": 2, "kapittel": "V1 Codering", "sectie": "V1.1", "eis": "Een eis."}
                ],
            }
        ),
        encoding="utf-8",
    )
    (bronmap / "asvs-zelfbeoordeling.yaml").write_text(
        yaml.safe_dump({"niveau": "L2", "oordelen": oordelen}), encoding="utf-8"
    )


@pytest.mark.parametrize("vlag", [[], ["--check"], ["--stdout"]])
def test_de_cli_meldt_een_eis_zonder_oordeel(vlag: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Een gat hoort een melding te geven, niet een KeyError uit het renderen."""
    _schrijf_bronnen(tmp_path, oordelen=[])
    afvinklijst = tmp_path / "asvs-zelfbeoordeling.md"
    monkeypatch.setattr("opi.tools.asvs_render.EISEN", tmp_path / "asvs-5.0-eisen.yaml")
    monkeypatch.setattr("opi.tools.asvs_render.BEOORDELING", tmp_path / "asvs-zelfbeoordeling.yaml")
    monkeypatch.setattr("opi.tools.asvs_render.AFVINKLIJST", afvinklijst)

    assert main(vlag) == 1
    assert not afvinklijst.exists()


def test_de_cli_schrijft_het_document_als_de_beoordeling_volledig_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _schrijf_bronnen(tmp_path, oordelen=[{"id": "V1.1.1", "status": "nvt", "toelichting": "geen eigen crypto"}])
    afvinklijst = tmp_path / "asvs-zelfbeoordeling.md"
    monkeypatch.setattr("opi.tools.asvs_render.EISEN", tmp_path / "asvs-5.0-eisen.yaml")
    monkeypatch.setattr("opi.tools.asvs_render.BEOORDELING", tmp_path / "asvs-zelfbeoordeling.yaml")
    monkeypatch.setattr("opi.tools.asvs_render.AFVINKLIJST", afvinklijst)

    assert main([]) == 0
    assert "V1.1.1" in afvinklijst.read_text(encoding="utf-8")


def test_de_cli_meldt_dat_de_lijst_actueel_is() -> None:
    assert main(["--check"]) == 0


def test_de_cli_meldt_drift_tussen_document_en_beoordeling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--check faalt als het document met de hand bewerkt of niet opnieuw gegenereerd is."""
    _schrijf_bronnen(tmp_path, oordelen=[{"id": "V1.1.1", "status": "nvt", "toelichting": "geen eigen crypto"}])
    afvinklijst = tmp_path / "asvs-zelfbeoordeling.md"
    afvinklijst.write_text("verouderde inhoud", encoding="utf-8")
    monkeypatch.setattr("opi.tools.asvs_render.EISEN", tmp_path / "asvs-5.0-eisen.yaml")
    monkeypatch.setattr("opi.tools.asvs_render.BEOORDELING", tmp_path / "asvs-zelfbeoordeling.yaml")
    monkeypatch.setattr("opi.tools.asvs_render.AFVINKLIJST", afvinklijst)

    assert main(["--check"]) == 1
    assert afvinklijst.read_text(encoding="utf-8") == "verouderde inhoud"


def test_de_cli_kan_printen_zonder_te_schrijven() -> None:
    voor = BEOORDELING.read_text(encoding="utf-8")
    assert main(["--stdout"]) == 0
    assert BEOORDELING.read_text(encoding="utf-8") == voor
