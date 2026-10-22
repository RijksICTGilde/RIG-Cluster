"""De BIV-inventaris is geldig, en het overzicht volgt er nog uit.

``architecture/biv-inventaris.yaml`` is de bron van waarheid voor de BIV-classificatie;
``architecture/biv-overzicht.md`` wordt eruit gegenereerd. Deze toetsen houden vast dat de
inventaris compleet is, dat diagram en tabel de inventaris dekken, en dat een gat (een
score H zonder mitigatie met toets, of een verdwenen toets) opvalt in plaats van stil te
blijven.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from opi.tools import biv_render
from opi.tools.biv_render import (
    ASSEN,
    INVENTARIS,
    OVERZICHT,
    REPO_ROOT,
    Inventaris,
    Score,
    aanvalsproef_doelen,
    gaten,
    load_inventaris,
    main,
    meldingen,
    ongedekte_aanvalsproef,
    render,
    render_diagram,
    render_tabel,
)
from pydantic import ValidationError


@pytest.fixture(scope="module")
def inventaris() -> Inventaris:
    return load_inventaris()


@pytest.fixture(scope="module")
def ruwe_inventaris() -> dict:
    return yaml.safe_load(INVENTARIS.read_text(encoding="utf-8"))


# --- 1. De inventaris als data -------------------------------------------------------


def test_inventaris_valideert(inventaris: Inventaris) -> None:
    """De eerste vulling is geldig: elke component heeft alle drie de assen en een BBN."""
    assert inventaris.componenten
    for component in inventaris.componenten:
        for as_naam in ASSEN:
            assert isinstance(component.score(as_naam), Score)
        assert component.bbn in (1, 2, 3)


def test_bbn_volgt_uit_de_hoogste_score(inventaris: Inventaris) -> None:
    verwacht = {Score.LAAG: 1, Score.MIDDEN: 2, Score.HOOG: 3}
    for component in inventaris.componenten:
        assert component.bbn == verwacht[component.hoogste_score]


def test_de_componenten_van_het_plan_staan_erin(inventaris: Inventaris) -> None:
    """De componenten uit de BIO/NORA-review zijn alle elf geclassificeerd."""
    verwacht = {
        "opi",
        "keycloak",
        "gedeelde-postgresql",
        "object-storage",
        "redis",
        "argocd",
        "gitops-repositories",
        "kopia-backups",
        "registry",
        "cert-manager-external-dns",
        "tenant-namespaces",
    }
    assert {component.id for component in inventaris.componenten} == verwacht


def test_as_zonder_mitigatie_wordt_geweigerd(ruwe_inventaris: dict) -> None:
    """Een score M of hoger zonder mitigatie op die as laat de validatie falen."""
    ruw = copy.deepcopy(ruwe_inventaris)
    ruw["componenten"][0]["mitigaties"]["integriteit"] = []

    with pytest.raises(ValidationError, match="geen mitigatie"):
        Inventaris.model_validate(ruw)


def test_onvolledige_entry_wordt_geweigerd(ruwe_inventaris: dict) -> None:
    """Een component zonder een van de drie assen is geen geldige entry."""
    ruw = copy.deepcopy(ruwe_inventaris)
    del ruw["componenten"][0]["vertrouwelijkheid"]

    with pytest.raises(ValidationError):
        Inventaris.model_validate(ruw)


def test_dubbele_id_wordt_geweigerd(ruwe_inventaris: dict) -> None:
    ruw = copy.deepcopy(ruwe_inventaris)
    ruw["componenten"].append(copy.deepcopy(ruw["componenten"][0]))

    with pytest.raises(ValidationError, match="dubbele component-id"):
        Inventaris.model_validate(ruw)


def test_onbekende_as_in_mitigaties_wordt_geweigerd(ruwe_inventaris: dict) -> None:
    """Een as-naam buiten ASSEN (bijvoorbeeld een typfout) is geen stille no-op."""
    ruw = copy.deepcopy(ruwe_inventaris)
    ruw["componenten"][0]["mitigaties"]["beschikbaarheidd"] = []

    with pytest.raises(ValidationError, match="onbekende as"):
        Inventaris.model_validate(ruw)


# --- 2. Het diagram volgt uit de inventaris ------------------------------------------


def test_elke_component_is_een_knoop(inventaris: Inventaris) -> None:
    diagram = render_diagram(inventaris)
    for component in inventaris.componenten:
        assert f'{component.id}["{component.naam}' in diagram


def test_elke_trust_boundary_is_een_subgraph(inventaris: Inventaris) -> None:
    diagram = render_diagram(inventaris)
    for boundary in inventaris.trust_boundaries:
        assert f'subgraph "{boundary}"' in diagram


def test_renderen_is_deterministisch(inventaris: Inventaris) -> None:
    assert render(inventaris) == render(inventaris)


def test_een_wijziging_in_de_inventaris_verandert_het_diagram(ruwe_inventaris: dict) -> None:
    """Geen handmatige bewerking die blijft staan: het diagram beweegt met de data mee."""
    ruw = copy.deepcopy(ruwe_inventaris)
    ruw["componenten"][0]["naam"] = "Een andere naam"
    gewijzigd = Inventaris.model_validate(ruw)

    assert render_diagram(gewijzigd) != render_diagram(load_inventaris())
    assert "Een andere naam" in render_diagram(gewijzigd)


def test_overzicht_loopt_niet_achter(inventaris: Inventaris) -> None:
    """Het gecommitte overzicht is exact wat de renderer nu maakt.

    Faalt dit, genereer opnieuw: ``task biv-overzicht``.
    """
    assert OVERZICHT.read_text(encoding="utf-8") == render(inventaris)


# --- 3. De tabel volgt uit dezelfde inventaris ---------------------------------------


def test_elke_component_heeft_precies_een_rij(inventaris: Inventaris) -> None:
    regels = render_tabel(inventaris).splitlines()
    # De kopregel met de kolomnamen en de scheidingsregel eronder horen niet bij de data.
    rijen = regels[2:]
    assert len(rijen) == len(inventaris.componenten)
    for component in inventaris.componenten:
        assert sum(1 for rij in rijen if rij.startswith(f"| {component.naam} |")) == 1


def test_mitigatielinks_zijn_niet_leeg_bij_m_of_hoger(inventaris: Inventaris) -> None:
    tabel = render_tabel(inventaris)
    for component in inventaris.componenten:
        for as_naam in ASSEN:
            if component.score(as_naam) is Score.LAAG:
                continue
            for mitigatie in component.mitigaties[as_naam]:
                assert f"[bron](../{mitigatie.bron})" in tabel
                if mitigatie.toets:
                    assert f"[toets](../{mitigatie.toets})" in tabel


# --- 4. De techniek bindt aan de inventaris ------------------------------------------


def test_de_eerste_vulling_heeft_geen_gaten(inventaris: Inventaris) -> None:
    assert gaten(inventaris) == []


def test_score_h_zonder_toets_is_een_gat(ruwe_inventaris: dict) -> None:
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["vertrouwelijkheid"] = "H"
    component["mitigaties"]["vertrouwelijkheid"] = [{"maatregel": "iets", "bron": "RC-370"}]

    meldingen = gaten(Inventaris.model_validate(ruw))

    assert meldingen == [f"{component['id']} / vertrouwelijkheid: score H zonder mitigatie met een toets"]


def test_verdwenen_toets_is_een_gat(ruwe_inventaris: dict) -> None:
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["mitigaties"]["integriteit"][0]["toets"] = "operations-manager/python/tests/test_weg.py"

    meldingen = gaten(Inventaris.model_validate(ruw))

    assert f"{component['id']} / integriteit: toets 'operations-manager/python/tests/test_weg.py' bestaat niet" in (
        meldingen
    )


def test_verdwenen_bron_is_een_gat(ruwe_inventaris: dict) -> None:
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["mitigaties"]["integriteit"][0]["bron"] = "plans/bestaat-niet.md"

    meldingen = gaten(Inventaris.model_validate(ruw))

    assert f"{component['id']} / integriteit: bron 'plans/bestaat-niet.md' bestaat niet" in meldingen


def test_de_check_meldt_niets_op_de_eerste_vulling(inventaris: Inventaris) -> None:
    """De CI-check draait groen op de inventaris zoals hij nu staat."""
    assert meldingen(inventaris) == []


def test_elke_genoemde_toets_bestaat(inventaris: Inventaris) -> None:
    for component in inventaris.componenten:
        for as_naam in ASSEN:
            for mitigatie in component.mitigaties[as_naam]:
                if mitigatie.toets:
                    assert (REPO_ROOT / mitigatie.toets).exists(), f"{component.id}: {mitigatie.toets}"


# --- 5. De aanvalsproef deelt de componentlijst --------------------------------------


def test_elke_component_met_score_h_is_een_doel_van_de_aanvalsproef(inventaris: Inventaris) -> None:
    assert ongedekte_aanvalsproef(inventaris) == []


def test_aanvalsproef_doelen_komen_uit_de_inventaris(inventaris: Inventaris) -> None:
    """Het harnas leest deze lijst; een tweede, losse lijst van onderdelen bestaat niet."""
    doelen = aanvalsproef_doelen(inventaris)

    assert doelen == sorted(doelen)
    assert set(doelen) <= {component.id for component in inventaris.componenten}
    assert set(doelen) >= {c.id for c in inventaris.componenten if c.hoogste_score is Score.HOOG}


def test_h_component_buiten_de_aanvalsproef_valt_op(ruwe_inventaris: dict) -> None:
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["aanvalsproef"] = False

    assert ongedekte_aanvalsproef(Inventaris.model_validate(ruw)) == [component["id"]]


# --- 6. De BIO-narratief verwijst, schrijft niet over --------------------------------


def test_de_bio_narratief_verwijst_naar_de_inventaris() -> None:
    """De scan-documenten leiden naar de inventaris in plaats van eigen scores te noemen."""
    for naam in ("technische-review-bio-en-nora.md", "technische-review-bio-en-nora-bevindingen.md"):
        tekst = (REPO_ROOT / "plans" / naam).read_text(encoding="utf-8")
        assert "architecture/biv-inventaris.yaml" in tekst


def test_het_overzicht_staat_in_architecture() -> None:
    assert Path(OVERZICHT).parent == REPO_ROOT / "architecture"


# --- main() (de CLI) ------------------------------------------------------------------
#
# load_inventaris() is hierbij gemockt zodat een test niet het echte, gecommitte
# overzicht kan overschrijven of de echte inventaris nodig heeft om een gat te tonen.
# OVERZICHT wordt per test naar een tmp_path-bestand verlegd.


def test_main_stdout_print_het_overzicht(
    inventaris: Inventaris, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: inventaris)

    exitcode = main(["--stdout"])

    assert exitcode == 0
    assert capsys.readouterr().out == render(inventaris)


def test_main_check_slaagt_zonder_gaten_en_zonder_drift(
    inventaris: Inventaris, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overzicht = tmp_path / "biv-overzicht.md"
    overzicht.write_text(render(inventaris), encoding="utf-8")
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: inventaris)
    monkeypatch.setattr(biv_render, "OVERZICHT", overzicht)

    exitcode = main(["--check"])

    assert exitcode == 0
    assert "zonder gaten" in capsys.readouterr().out


def test_main_check_faalt_bij_een_gat(
    ruwe_inventaris: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["vertrouwelijkheid"] = "H"
    component["mitigaties"]["vertrouwelijkheid"] = [{"maatregel": "iets", "bron": "RC-370"}]
    met_gat = Inventaris.model_validate(ruw)
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: met_gat)
    monkeypatch.setattr(biv_render, "OVERZICHT", tmp_path / "biv-overzicht.md")

    exitcode = main(["--check"])

    assert exitcode == 1
    assert "zonder mitigatie met een toets" in capsys.readouterr().err


def test_main_check_faalt_bij_drift_op_het_overzicht(
    inventaris: Inventaris, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overzicht = tmp_path / "biv-overzicht.md"
    overzicht.write_text("verouderde inhoud", encoding="utf-8")
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: inventaris)
    monkeypatch.setattr(biv_render, "OVERZICHT", overzicht)

    exitcode = main(["--check"])

    assert exitcode == 1
    assert "loopt achter op de inventaris" in capsys.readouterr().err


def test_main_schrijft_het_overzicht_zonder_vlag(
    inventaris: Inventaris, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overzicht = tmp_path / "biv-overzicht.md"
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: inventaris)
    monkeypatch.setattr(biv_render, "OVERZICHT", overzicht)

    exitcode = main([])

    assert exitcode == 0
    assert overzicht.read_text(encoding="utf-8") == render(inventaris)
    uitvoer = capsys.readouterr()
    assert "schreef" in uitvoer.out
    assert uitvoer.err == ""


def test_main_zonder_vlag_meldt_gaten_op_stderr_maar_schrijft_toch(
    ruwe_inventaris: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Het overzicht volgt uit de inventaris, ook met een gat: het gat is zichtbaar, niet stil."""
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["vertrouwelijkheid"] = "H"
    component["mitigaties"]["vertrouwelijkheid"] = [{"maatregel": "iets", "bron": "RC-370"}]
    met_gat = Inventaris.model_validate(ruw)
    overzicht = tmp_path / "biv-overzicht.md"
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: met_gat)
    monkeypatch.setattr(biv_render, "OVERZICHT", overzicht)

    exitcode = main([])

    assert exitcode == 0
    assert overzicht.read_text(encoding="utf-8") == render(met_gat)
    assert "zonder mitigatie met een toets" in capsys.readouterr().err


def test_main_check_toetst_ook_met_stdout(
    ruwe_inventaris: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--stdout mag de check niet overslaan: dan meldt een gat zich niet in CI."""
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["vertrouwelijkheid"] = "H"
    component["mitigaties"]["vertrouwelijkheid"] = [{"maatregel": "iets", "bron": "RC-370"}]
    met_gat = Inventaris.model_validate(ruw)
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: met_gat)
    monkeypatch.setattr(biv_render, "OVERZICHT", tmp_path / "biv-overzicht.md")

    exitcode = main(["--check", "--stdout"])

    uitvoer = capsys.readouterr()
    assert exitcode == 1
    assert uitvoer.out == render(met_gat)
    assert "zonder mitigatie met een toets" in uitvoer.err


def test_main_check_met_stdout_schrijft_het_overzicht_niet(
    inventaris: Inventaris, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overzicht = tmp_path / "biv-overzicht.md"
    overzicht.write_text(render(inventaris), encoding="utf-8")
    monkeypatch.setattr(biv_render, "load_inventaris", lambda: inventaris)
    monkeypatch.setattr(biv_render, "OVERZICHT", overzicht)

    assert main(["--check", "--stdout"]) == 0


# --- De tabel blijft een tabel ---------------------------------------------------------


def test_een_pijp_of_regelafbreking_breekt_de_tabel_niet(ruwe_inventaris: dict) -> None:
    """Een | sluit anders de kolom en een regelafbreking de rij."""
    ruw = copy.deepcopy(ruwe_inventaris)
    component = ruw["componenten"][0]
    component["gegevens"] = "sleutels | wachtwoorden\nen sessies"
    component["mitigaties"]["integriteit"][0]["maatregel"] = "a | b\nc"
    kolommen = 10

    tabel = render_tabel(Inventaris.model_validate(ruw))
    rijen = tabel.splitlines()[2:]

    assert len(rijen) == len(ruw["componenten"])
    for rij in rijen:
        assert rij.count("|") - rij.count("\\|") == kolommen + 1
    assert "sleutels \\| wachtwoorden<br/>en sessies" in tabel
    assert "a \\| b<br/>c" in tabel


# --- De repo-root zonder vaste diepte -------------------------------------------------


def test_vind_repo_root_vindt_de_checkout(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    (root / "architecture").mkdir(parents=True)
    (root / biv_render.INVENTARIS_VANAF_ROOT).write_text("componenten: []", encoding="utf-8")
    diep = root / "a" / "b" / "c"
    diep.mkdir(parents=True)

    assert biv_render.vind_repo_root(diep / "biv_render.py") == root


def test_vind_repo_root_weigert_buiten_een_checkout(tmp_path: Path) -> None:
    """In de image staat de code op /app/opi/tools: geen checkout, dus een duidelijke melding."""
    image_pad = tmp_path / "app" / "opi" / "tools" / "biv_render.py"
    image_pad.parent.mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="geen checkout rond"):
        biv_render.vind_repo_root(image_pad)
