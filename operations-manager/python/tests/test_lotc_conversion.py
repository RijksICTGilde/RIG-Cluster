"""Bewaakt hoever de omzetting naar LOTC staat.

De omgezette templates in ``opi/templates_lotc/`` worden gegenereerd door
``scripts/lotc_convert_templates.py``. Deze test toetst het enige dat zonder
paginadata te toetsen valt, en dat is meer dan het lijkt: LOTC valideert bij het
COMPILEREN al of elk component bestaat en of elk attribuut bij dat component hoort.
Een template dat compileert, gebruikt dus aantoonbaar een bestaande woordenschat.

Waarom een lijst met bekende uitzonderingen en geen simpele "alles moet compileren":
de resterende templates falen allemaal op hetzelfde, en dat is niets wat wij in een
template kunnen repareren. Onze formulierwidgets bouwen attributen voorwaardelijk op
met macro's die of niets of een heel attribuut teruggeven:

    <c-text-input-field id="..." {{ bool_attr("required", field.required) }} />

De roos-parser laat dat door, de LOTC-parser leest de haakjes als attribuutnaam. Dat
is bij het LOTC-project neergelegd. Tot het opgelost is horen deze bestanden hier, met
hun aantal, zodat twee dingen opvallen: dat de lijst groeit (er is iets kapot gegaan)
en dat hij krimpt (er kan iets van af).
"""

from pathlib import Path

import pytest

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"

# Templates die nog niet compileren omdat ze Jinja-expressies op attribuutpositie
# gebruiken. Bijna allemaal de formulierlaag; zie de moduledocstring.
KNOWN_UNCONVERTED = {
    "invite-register.html.j2",
    "widgets/checkbox.html.j2",
    "widgets/checkbox_group.html.j2",
    "widgets/column.html.j2",
    "widgets/date.html.j2",
    "widgets/fieldset.html.j2",
    "widgets/form_start.html.j2",
    "widgets/number.html.j2",
    "widgets/preset_cards.html.j2",
    "widgets/radio.html.j2",
    "widgets/row.html.j2",
    "widgets/select.html.j2",
    "widgets/sequence.html.j2",
    "widgets/sequence_item_card.html.j2",
    "widgets/sequence_item_inline.html.j2",
    "widgets/service_cards.html.j2",
    "widgets/submit.html.j2",
    "widgets/text.html.j2",
    "widgets/textarea.html.j2",
    "wizard/modal_wizard_review.html.j2",
}


def _compile_all() -> tuple[set[str], dict[str, str]]:
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd (dependency-group lotc)")
    from opi.core.templates_lotc import templates_lotc

    compiled: set[str] = set()
    failed: dict[str, str] = {}
    for path in sorted(TEMPLATES_LOTC_DIR.rglob("*.j2")):
        name = str(path.relative_to(TEMPLATES_LOTC_DIR))
        try:
            templates_lotc.env.get_template(name)
        except Exception as error:
            failed[name] = str(error).split(" at line")[0]
        else:
            compiled.add(name)
    return compiled, failed


def test_conversion_state_is_unchanged() -> None:
    """Precies de bekende uitzonderingen falen - niet meer en niet minder."""
    _, failed = _compile_all()
    assert set(failed) == KNOWN_UNCONVERTED, (
        f"nieuw kapot: {sorted(set(failed) - KNOWN_UNCONVERTED)}; "
        f"gerepareerd (haal uit KNOWN_UNCONVERTED): {sorted(KNOWN_UNCONVERTED - set(failed))}"
    )


def test_the_shell_and_its_pages_compile() -> None:
    """De schil en alles wat hem uitbreidt doen het, want daar draait de proef op."""
    compiled, failed = _compile_all()
    assert "base_lotc.html.j2" in compiled, f"de schil compileert niet: {failed.get('base_lotc.html.j2')}"

    extending = [
        str(path.relative_to(TEMPLATES_LOTC_DIR))
        for path in TEMPLATES_LOTC_DIR.rglob("*.j2")
        if 'extends "base_lotc.html.j2"' in path.read_text()
    ]
    assert extending, "geen enkel template breidt de LOTC-schil uit"
    broken = [name for name in extending if name in failed]
    assert not broken, f"paginas op de LOTC-schil die niet compileren: {broken}"


def test_our_own_components_are_registered() -> None:
    """Wat ZAD zelf levert zolang LOTC het niet heeft, is aanroepbaar."""
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    from opi.core.templates_lotc import templates_lotc

    rendered = templates_lotc.env.from_string('<c-secret-field value="geheim" />').render()
    assert "zad-secret-field" in rendered
    assert "lotc-unimplemented" not in rendered
    assert "geheim" in rendered
