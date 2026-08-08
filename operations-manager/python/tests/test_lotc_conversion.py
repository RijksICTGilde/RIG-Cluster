"""Bewaakt hoever de omzetting naar LOTC staat.

De omgezette templates in ``opi/templates_lotc/`` worden gegenereerd door
``scripts/lotc_convert_templates.py``. Deze test toetst het enige dat zonder
paginadata te toetsen valt, en dat is meer dan het lijkt: LOTC valideert bij het
COMPILEREN al of elk component bestaat en of elk attribuut bij dat component hoort.
Een template dat compileert, gebruikt dus aantoonbaar een bestaande woordenschat.

Waarom een lijst met bekende uitzonderingen en geen simpele "alles moet compileren":
er is er nog een, en die kunnen wij niet zelf oplossen. De lijst bewaakt twee kanten op:
groeit hij, dan is er iets kapot gegaan; krimpt hij, dan kan er iets af.
"""

from pathlib import Path

import pytest

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"

KNOWN_UNCONVERTED = {
    # De enige die overblijft, en niet door ons op te lossen: hij gebruikt c-data-list,
    # en NLDD implementeert dat niet. Eerder beeldde de omzetter dat af op c-detail-list
    # omdat DIE wel rendert, maar het LOTC-project wees erop dat het twee verschillende
    # dingen zijn: data-list is een definitielijst (<dl>), detail-list een rijkere lijst
    # met een eigen structuur. Een pagina die rendert maar iets anders toont is erger dan
    # een pagina die niet rendert, dus die afbeelding is teruggedraaid. Een aanroep in
    # totaal; LOTC heeft data-list onder NLDD op de lijst staan.
    "roos-form-improved.html.j2",
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


def test_secret_field_comes_from_lotc() -> None:
    """c-secret-field wordt door LOTC zelf geleverd, niet meer door ons.

    Wij hadden hier een tijdelijke eigen versie omdat LOTC hem nog niet had. Deze test
    houdt vast dat de opruiming klopt: de component bestaat, rendert niet als placeholder,
    en komt niet uit onze eigen map.
    """
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    from opi.core.templates_lotc import templates_lotc

    rendered = templates_lotc.env.from_string('<c-secret-field value="geheim" />').render()
    assert "lotc-unimplemented" not in rendered, "c-secret-field is niet geimplementeerd"
    assert "zad-secret-field" not in rendered, "er staat nog een eigen versie in de weg"
    assert "geheim" in rendered


def test_form_widgets_render_through_the_lotc_adapter() -> None:
    """De omgezette widgets worden ook echt gebruikt, en leveren NLDD-markup.

    Zonder deze toets zouden de widget-templates kunnen compileren zonder dat er iets
    ze aanroept - dan is de formulierlaag omgezet op papier en niet in de applicatie.
    """
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    from types import SimpleNamespace

    from opi.forms.widgets.lotc import LOTCWidgetAdapter

    field = SimpleNamespace(
        path="naam",
        label="Projectnaam",
        placeholder="mijn-project",
        required=True,
        readonly=False,
        value="waarde",
        errors=[],
        warnings=[],
        help_text="Alleen kleine letters",
        description=None,
        help_template=None,
        widget_type="text",
        htmx_attrs={"hx-get": "/controleer"},
        attributes={"data-q": "1", "converter": "hoort-er-niet-in"},
        options=None,
    )
    rendered = LOTCWidgetAdapter().render_text(field)

    assert "nldd-text-field" in rendered, "het veld rendert niet als NLDD-component"
    # De hulptekst hoort een eigen element te zijn dat aan het invoerveld gekoppeld is;
    # dat is de toegankelijkheidswinst die roos hier niet levert.
    assert "nldd-form-field-help-text" in rendered
    # De attribuutbundel landt op het invoerveld...
    assert 'hx-get="/controleer"' in rendered
    assert 'data-q="1"' in rendered
    # ...maar velddefinitie-instellingen horen niet in de HTML.
    assert "converter" not in rendered
