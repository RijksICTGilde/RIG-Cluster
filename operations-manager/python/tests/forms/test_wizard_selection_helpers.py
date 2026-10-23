"""De bouwstenen van een selectie zelf (RC-378): ``opi.forms.wizard.selection``.

De scenario's via ``apply_modal_edit`` staan in ``test_component_modal_removal_and_selection.py``
en ``test_modal_noop_roundtrip.py``; dit meet de losse functies op hun randen: hoe de
query-string wordt gelezen, en wat ``splice_selection`` doet met een item dat de gebruiker
TOEVOEGDE terwijl de modal maar een selectie droeg.
"""

from __future__ import annotations

from typing import Any

from opi.forms.visualizers.flows import ListSelectable
from opi.forms.wizard.selection import parse_selection, splice_selection


def test_lege_en_ontbrekende_query_geeft_lege_lijst() -> None:
    assert parse_selection(None) == []
    assert parse_selection("") == []
    assert parse_selection("   ") == []


def test_spaties_en_dubbelen_worden_opgeruimd() -> None:
    assert parse_selection(" alpha , beta ,alpha") == ["alpha", "beta"]


def test_een_losse_komma_levert_geen_lege_naam() -> None:
    assert parse_selection("alpha,,beta,") == ["alpha", "beta"]


SELECTABLE = ListSelectable("components", "name")


def _project(names: list[str]) -> dict[str, Any]:
    return {"components": [{"name": n, "ports": {"inbound": [8000]}} for n in names]}


def test_een_toegevoegd_item_in_de_selectie_komt_achteraan() -> None:
    """De modal droeg alleen 'beta', maar de gebruiker voegde ook 'nieuw' toe via de
    sequence-editor. Dat nieuwe item hoort niet een bestaande plek te verdringen."""
    existing = _project(["alpha", "beta", "gamma"])
    merged = _project(["alpha", "beta", "gamma"])
    merged["components"] = [{"name": "beta", "ports": {"inbound": [9000]}}, {"name": "nieuw"}]

    splice_selection(existing, merged, SELECTABLE, ["beta"])

    names = [c["name"] for c in merged["components"]]
    assert names == ["alpha", "beta", "gamma", "nieuw"]
    assert merged["components"][1]["ports"] == {"inbound": [9000]}


def test_het_eerste_item_van_de_selectie_weghalen_verschuift_de_rest_niet() -> None:
    """De andere kant van de toets hieronder: niet het laatste maar het EERSTE item van
    de selectie valt weg. Op positie paren liet 'gamma' dan op de plek van 'alpha'
    landen, voor 'beta'."""
    existing = _project(["alpha", "beta", "gamma", "delta"])
    merged = _project(["alpha", "beta", "gamma", "delta"])
    merged["components"] = [{"name": "gamma"}]

    splice_selection(existing, merged, SELECTABLE, ["alpha", "gamma"])

    names = [c["name"] for c in merged["components"]]
    assert names == ["beta", "gamma", "delta"]


def test_verwijderde_items_blijven_weg_en_laten_geen_gat() -> None:
    """Twee van de drie geselecteerde items opgeslagen: het derde is weggehaald, en de
    rest van de lijst verschuift niet naar zijn plek."""
    existing = _project(["alpha", "beta", "gamma", "delta"])
    merged = _project(["alpha", "beta", "gamma", "delta"])
    merged["components"] = [{"name": "alpha"}]

    splice_selection(existing, merged, SELECTABLE, ["alpha", "gamma"])

    names = [c["name"] for c in merged["components"]]
    assert names == ["alpha", "beta", "delta"]
