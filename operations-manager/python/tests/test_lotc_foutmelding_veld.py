"""De foutmelding bij een formulierveld: staat hij er, en is hij te ZIEN?

Een melding die in de DOM staat en ``display: none`` is, is geen melding. Dat was de
vondst die deze poort opleverde: twee foutregels met de juiste tekst, allebei hoogte 0,
en de gebruiker zag een rood kader zonder te weten wat er mis was.

NLDD 0.8.84 heeft dat mechanisme vervangen. ``nldd-form-field-error-text`` en
``error-message-ids`` zijn weg; een serverfout is nu een eis waar de waarde niet aan
voldoet, en die staat in een ``nldd-validation-list``. De lijst vraagt de BESTURING welke
eisen onvervuld zijn: een item is zichtbaar als de besturing ``invalid`` draagt EN het id
van het item in haar ``unmet`` staat. ``judging`` zet de lijst zelf; met de hand gezet
maakt het juist elke melding onzichtbaar.

Wat hier bewaakt wordt:

- die bedrading, op de HTML die de veldsjablonen opleveren, per veldsoort;
- dat onze drie kopieen in ``opi/templates_lotc/components/`` op precies de bedoelde
  punten van de geinstalleerde afwijken, zodat een nieuwe versie van lotc-forms hier
  opvalt en niet stilletjes langs ons heen gaat.

Dat de melding in een BROWSER ook echt hoogte heeft, staat in
``tests/e2e/test_lotc_veldfout_zichtbaar.py``. Dat is de meting die telt: deze test leest
markup, en markup kan er goed uitzien terwijl het scherm een leeg vak tekent.
"""

from __future__ import annotations

from pathlib import Path

import lotc_forms
import pytest
from opi.core.templates_lotc import templates_lotc

#: De veldsoorten die hun serverfout zelf bedraden, met de tag van de besturing. De
#: losse-aankruisvakje-tak staat er als laatste bij: die rendert GEEN ``nldd-form-field``
#: maar een eigen ``<div>``, en juist dat is de reden dat onze kopie hem ``for`` geeft.
VELDEN = [
    ("c-text-input-field", "nldd-text-field"),
    ("c-textarea-field", "nldd-multi-line-text-field"),
    ("c-date-input-field", "nldd-date-field"),
    ("c-file-input-field", "nldd-file-field"),
    ("c-select-field", "nldd-combo-box"),
    ("c-checkbox-field", "nldd-checkbox-field"),
]


def _render(tag: str, *, error: str | None = "Dit veld is verplicht", extra: str = "") -> str:
    fout = f' error="{error}"' if error else ""
    bron = f'<{tag} id="veld" name="veld" label="Naam"{fout}{extra}/>'
    return templates_lotc.env.from_string(bron).render()


@pytest.mark.parametrize(("tag", "besturing"), VELDEN)
def test_de_besturing_draagt_invalid_en_unmet(tag: str, besturing: str) -> None:
    """Zonder allebei toont de lijst haar item niet, ook al staat de tekst er."""
    html = _render(tag)
    assert f"<{besturing} " in html, f"{tag} rendert geen {besturing}"
    kop = html.split(f"<{besturing} ", 1)[1].split(">", 1)[0]
    assert " invalid" in kop, f"{tag}: de besturing mist invalid"
    assert 'unmet="veld-error"' in kop, f"{tag}: de besturing mist unmet met het id van het item"


@pytest.mark.parametrize(("tag", "besturing"), VELDEN)
def test_het_item_draagt_het_id_waar_unmet_naar_wijst(tag: str, besturing: str) -> None:
    """De lijst matcht op id; een item zonder id komt nooit in de unmet-lijst voor."""
    del besturing
    html = _render(tag)
    assert '<nldd-validation-item id="veld-error">' in html, f"{tag} mist het foutitem"
    assert "Dit veld is verplicht" in html


@pytest.mark.parametrize(("tag", "besturing"), VELDEN)
def test_judging_wordt_niet_met_de_hand_gezet(tag: str, besturing: str) -> None:
    """De lijst latcht judging zelf uit de besturing.

    Staat het in de markup, dan is ``visible`` al bij het eerste renderen vastgezet en
    blijft de melding onzichtbaar. Een fout die er precies zo uitziet als een goede.
    """
    del besturing
    assert "judging" not in _render(tag)


@pytest.mark.parametrize(("tag", "besturing"), VELDEN)
def test_zonder_fout_geen_bedrading(tag: str, besturing: str) -> None:
    """Een veld zonder fout blijft precies zoals het was."""
    del besturing
    html = _render(tag, error=None)
    assert "unmet" not in html
    assert "validation-item" not in html
    assert "form-field-error-text" not in html


def test_de_losse_aankruisvakje_tak_wijst_zijn_lijst_naar_de_besturing() -> None:
    """De enige veldsoort zonder ``nldd-form-field`` eromheen.

    ``nldd-form-field`` is wat een lijst zonder ``for`` aan de besturing knoopt. Deze tak
    heeft die ouder niet, dus zonder ``for`` leest de lijst nergens ``invalid`` of
    ``unmet`` en tekent ze een leeg vak. Gemeten in een browser: zonder ``for`` 0 px, met
    ``for`` 20 px.
    """
    html = _render("c-checkbox-field")
    assert "<nldd-form-field " not in html, "de losse tak rendert geen nldd-form-field meer"
    assert '<nldd-validation-list for="veld">' in html


def test_de_groepstakken_hebben_het_gebrek_nog_en_dat_staat_opgeschreven() -> None:
    """De keerzijde: waar de reparatie NIET zit, en waarom dat mag.

    Bij een aankruisvakje-GROEP en bij radioknoppen hangt de lijst wel aan een
    ``nldd-form-field``, maar die knoopt haar aan het eerste invoerelement BINNEN de
    omhulling en niet aan de omhulling zelf. De reparatie hoort dus in lotc-forms; het
    verzoek staat in request_for_components.md.

    Geen veld van dit portaal met die twee widgets is ``required`` of heeft een validator,
    dus er is vandaag geen weg naar een serverfout op zo'n veld. Repareert lotc-forms het,
    dan valt deze test om en kan hij weg.
    """
    groep = templates_lotc.env.from_string(
        '<c-checkbox-field id="veld" label="Naam" error="Kies er een">'
        '<c-checkbox name="veld" value="a" label="A"/></c-checkbox-field>'
    ).render()
    omhulling = groep.split("<div", 1)[1].split(">", 1)[0]
    assert "unmet" not in omhulling, "de groepsomhulling is bedraad; werk deze test en het verzoek bij"

    radio = _render("c-radio-button-field")
    assert 'unmet="veld-error"' not in radio, "radio is bedraad; werk deze test en het verzoek bij"


# --------------------------------------------------------------------------------------
# De kopieen naast het origineel
# --------------------------------------------------------------------------------------

#: De sjabloonmap van de geinstalleerde lotc-forms.
LOTC_FORMS = Path(str(lotc_forms.__file__)).parent / "templates" / "components"

#: Onze map met kopieen ervan.
ONZE_MAP = Path(__file__).resolve().parent.parent / "opi" / "templates_lotc" / "components"

#: De regel waarachter onze toelichting ophoudt en de kopie begint.
STREEP = "---- vanaf hier de kopie ---- #}\n"

#: Per kopie: wat lotc-forms schrijft, en wat wij ervan maken. Ze worden op volgorde
#: toegepast, dus een latere vervanging werkt op wat een eerdere opleverde.
#:
#: _forms.j2: de besturing wordt EEN keer gerenderd zodat het frame hem kan lezen; de
#: openingstag laat "Optioneel" weg als het veld daarom vraagt; en een keuzelijst krijgt
#: hem nooit, want daar staat altijd al iets geselecteerd en bij een select landt het merk
#: op de omhulling in plaats van op de besturing.
#:
#: select-field / checkbox-field: de foutbedrading die lotc-forms in deze takken vergat.
VERVANGINGEN = {
    "_forms.j2": [
        (
            "{% macro nldd_field(id, label, help, error, required, kind) -%}\n",
            "{% macro nldd_field(id, label, help, error, required, kind) -%}\n{% set besturing = caller() %}\n",
        ),
        (
            '<nldd-form-field label="{{ label }}"{% if not required %} optional{% endif %}',
            '<nldd-form-field label="{{ label }}"'
            "{% if not required and 'data-no-optional-badge' not in besturing %} optional{% endif %}",
        ),
        (
            "{% if not required and 'data-no-optional-badge' not in besturing %}",
            "{% if not required and kind != 'select-field' and 'data-no-optional-badge' not in besturing %}",
        ),
        (
            "  {{ caller() }}\n  {% if help %}<nldd-form-field-help-text",
            "  {{ besturing }}\n  {% if help %}<nldd-form-field-help-text",
        ),
    ],
    "select-field.html.j2": [
        (
            '{% if error %} aria-invalid="true"{% endif %}{% if required %} required{% endif %}'
            "{% if disabled %} disabled{% endif %}{{ f.render_attrs(c) }}>\n"
            '{% if placeholder %}<option value="">{{ placeholder }}</option>{% endif %}'
            "{{ content | safe }}</select>\n{% endcall %}\n{% elif",
            '{% if error %} aria-invalid="true" invalid unmet="{{ id }}-error"{% endif %}'
            "{% if required %} required{% endif %}{% if disabled %} disabled{% endif %}"
            '{{ f.render_attrs(c) }}>\n{% if placeholder %}<option value="">{{ placeholder }}</option>{% endif %}'
            "{{ content | safe }}</select>\n{% endcall %}\n{% elif",
        ),
        (
            '<nldd-combo-box id="{{ id }}" name="{{ name }}"{% if value %} value="{{ value }}"{% endif %}'
            "{% if error %} invalid{% endif %}",
            '<nldd-combo-box id="{{ id }}" name="{{ name }}"{% if value %} value="{{ value }}"{% endif %}'
            '{% if error %} invalid unmet="{{ id }}-error"{% endif %}',
        ),
    ],
    "checkbox-field.html.j2": [
        (
            '{% if error %}<nldd-validation-list><nldd-validation-item id="{{ id }}-error">'
            "{{ error }}</nldd-validation-item></nldd-validation-list>{% endif %}\n</div>",
            '{% if error %}<nldd-validation-list for="{{ id }}"><nldd-validation-item id="{{ id }}-error">'
            "{{ error }}</nldd-validation-item></nldd-validation-list>{% endif %}\n</div>",
        ),
    ],
}


@pytest.mark.parametrize("naam", sorted(VERVANGINGEN))
def test_onze_kopie_wijkt_op_precies_die_punten_af(naam: str) -> None:
    """Verandert lotc-forms iets anders in dit bestand, dan faalt dit - en niet de UI.

    Onze kopieen liggen op de searchpath VOOR de sjablonen van de design systems en winnen
    dus van het origineel. Dat is een prima manier om een gebrek in het thema te
    overbruggen en een slechte manier om een verbetering te MISSEN, dus worden ze hier
    naast elkaar gelegd.
    """
    origineel = (LOTC_FORMS / naam).read_text()
    _, streep, kopie = (ONZE_MAP / naam).read_text().partition(STREEP)
    assert streep, f"{naam}: de markering die onze toelichting van de kopie scheidt is weg"

    verwacht = origineel
    for van, naar in VERVANGINGEN[naam]:
        assert van in verwacht, f"lotc-forms schrijft {naam} anders; deze regel is weg: {van[:70]}"
        verwacht = verwacht.replace(van, naar, 1)

    assert kopie == verwacht, f"lotc-forms heeft components/{naam} gewijzigd; loop onze kopie na"


@pytest.mark.parametrize("naam", sorted(VERVANGINGEN))
def test_de_kopie_wint_van_de_geinstalleerde(naam: str) -> None:
    """Zonder deze volgorde doen onze kopieen niets en vallen de reparaties stil weg."""
    geladen = templates_lotc.env.get_template(f"components/{naam}").filename
    assert geladen == str(ONZE_MAP / naam), f"components/{naam} komt van {geladen}"


def test_het_origineel_heeft_de_gebreken_nog() -> None:
    """De reden dat wij deze kopieen hebben. Weg? Dan kunnen de kopieen ook weg."""
    forms = (LOTC_FORMS / "_forms.j2").read_text()
    assert "no-optional-badge" not in forms, (
        "lotc-forms kent de badge-stand nu zelf; haal onze kopie van components/_forms.j2 weg"
    )

    select = (LOTC_FORMS / "select-field.html.j2").read_text()
    assert "unmet" not in select, (
        "lotc-forms bedraadt de keuzelijst nu zelf; haal onze kopie van components/select-field.html.j2 weg"
    )

    checkbox = (LOTC_FORMS / "checkbox-field.html.j2").read_text()
    assert "<nldd-validation-list for=" not in checkbox, (
        "lotc-forms wijst de lijst nu zelf aan; haal onze kopie van components/checkbox-field.html.j2 weg"
    )
