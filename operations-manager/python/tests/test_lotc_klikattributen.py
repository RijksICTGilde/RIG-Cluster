"""Jinja in een ``@click``-waarde: wat er wel en niet doorheen komt.

WAT HIER OOIT MISGING

Gemeten op ``/admin/approvals``: de knop "Beheren" stond in het sjabloon als

    <c-button ... @click="openApprovalModal('{{ project.project_name }}')" />

en kwam er als

    <nldd-button ... onclick="openApprovalModal('{{ project.project_name }}')">

uit - met de accolades en al. De componentlaag nam de waarde van een ``@``-afhandelaar
LETTERLIJK uit de bron over. Wat de gebruiker daarvan merkte: de kop van de dialoog las
"Domeingoedkeuring - {{ project.project_name }}", en het formulier werd opgehaald bij
``/admin/approvals/%7B%7B%20project.project_name%20%7D%7D/modal-wizard/admin-approval``.
Geen foutmelding, geen 500 - een 404 op een projectnaam die niemand heeft.

DAT IS OPGELOST, EN ER IS IETS ANDERS VOOR IN DE PLAATS GEKOMEN

Sinds NLDD 0.8.92 rendert de componentlaag de waarde van een ``@``-afhandelaar net als
elke andere attribuutwaarde. De omweg via een data-attribuut is dus niet meer nodig; de
metingen onderaan dit bestand leggen dat vast, zodat de dag dat het weer omslaat hier
opvalt en niet op een scherm.

Wat er voor in de plaats komt is de ESCAPING. De waarde komt in een attribuut tussen
dubbele aanhalingstekens te staan, dus een dubbel aanhalingsteken IN die waarde sluit het
attribuut voortijdig. Dat gebeurt precies bij de waarden die je met ``tojson`` bouwt, en
``| e`` helpt niet: ``tojson`` levert ``Markup`` op en dat filter laat markup met rust.
``| forceescape`` is de afsluiting die wel werkt. Zie
``tests/test_lotc_attribuutwaarden.py`` voor die meting; hier staat de regel die hem over
alle sjablonen afdwingt.
"""

from __future__ import annotations

import pathlib
import re

from opi.core.template_helpers import CATALOG_DIR, TEMPLATES_DIR
from opi.core.templates_lotc import templates_lotc

#: Een ``@``-afhandelaar die zijn hele waarde uit een variabele haalt: ``@click="{{ x }}"``.
KLIK_UIT_VARIABELE = re.compile(r'@([a-z]+)\s*=\s*"\{\{\s*([^}]+?)\s*\}\}"')

#: ``{% set naam %}...{% endset %}`` of ``{% set naam = ... %}``, met de inhoud.
SET_BLOK = "{{%\\s*set\\s+{naam}\\s*%}}(.*?){{%\\s*endset\\s*%}}"
SET_INLINE = "{{%\\s*set\\s+{naam}\\s*=\\s*(.+?)%}}"


def _templatebestanden() -> list[pathlib.Path]:
    """Alle Jinja-templates van het portaal: de eigen map plus die van de diensten."""
    bestanden: list[pathlib.Path] = []
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        bestanden.extend(pathlib.Path(map_).rglob("*.html.j2"))
    return bestanden


def _bron_van_de_waarde(tekst: str, naam: str) -> str:
    """De ``{% set %}`` die deze variabele vult, of een lege string als hij hier niet staat."""
    for patroon in (SET_BLOK, SET_INLINE):
        treffer = re.search(patroon.format(naam=re.escape(naam)), tekst, re.DOTALL)
        if treffer:
            return treffer.group(1)
    return ""


def test_een_json_waarde_in_een_klikafhandelaar_gaat_langs_forceescape() -> None:
    """Zonder dat filter sluit het ``onclick``-attribuut bij het eerste teken uit de JSON.

    ``tojson`` is de enige plek waar deze sjablonen een dubbel aanhalingsteken in een
    afhandelaar krijgen, dus dat is waar de regel op aanslaat. Een aanroep met alleen
    enkele aanhalingstekens erin heeft het filter niet nodig en krijgt het ook niet.
    """
    kaal = []
    for pad in _templatebestanden():
        tekst = pad.read_text()
        for treffer in KLIK_UIT_VARIABELE.finditer(tekst):
            naam = treffer.group(2).split("|")[0].strip()
            bron = _bron_van_de_waarde(tekst, naam) + " " + treffer.group(2)
            if "tojson" in bron and "forceescape" not in bron:
                kaal.append(f'{pad}: @{treffer.group(1)}="{{{{ {naam} }}}}"')

    assert kaal == [], (
        "Deze afhandelaars bouwen hun waarde met tojson zonder forceescape. tojson levert "
        "Markup op, en het escapen dat de componentlaag doet laat markup met rust, dus de "
        "aanhalingstekens uit de JSON belanden rauw in het onclick-attribuut en sluiten "
        "dat voortijdig. Zet er | forceescape achter:\n  " + "\n  ".join(kaal)
    )


# --------------------------------------------------------------------------------------
# De metingen waar de regel hierboven op rust
# --------------------------------------------------------------------------------------


def _onclick(bron: str, **context: object) -> str:
    html = templates_lotc.env.from_string(bron).render(**context)
    treffer = re.search(r'onclick="([^"]*)', html)
    assert treffer, f"geen onclick in de uitvoer: {html[:200]}"
    return treffer.group(1)


def test_een_mustache_in_een_afhandelaar_wordt_wel_gerenderd() -> None:
    """De bug uit de kop van dit bestand. Komt hij terug, dan faalt dit.

    Met de vorige pin stond hier ``openApprovalModal('{{ naam }}')`` in het onclick, met
    de accolades en al. Nu staat de waarde erin.
    """
    uit = _onclick('<c-button label="x" @click="openApprovalModal(\'{{ naam }}\')" />', naam="mijn-project")

    assert uit == "openApprovalModal('mijn-project')"
    assert "{{" not in uit


def test_een_jinja_statement_in_een_afhandelaar_wordt_ook_gerenderd() -> None:
    """Niet alleen ``{{ }}``: ook een ``{% if %}`` in de waarde draait."""
    bron = '<c-button label="x" @click="f(\'{% if aan %}ja{% else %}nee{% endif %}\')" />'

    assert _onclick(bron, aan=True) == "f('ja')"
    assert _onclick(bron, aan=False) == "f('nee')"


def test_een_json_waarde_zonder_forceescape_breekt_het_attribuut() -> None:
    """De keerzijde van de regel bovenaan: zonder het filter sluit het attribuut te vroeg.

    Zonder deze helft zou die regel een afspraak zijn zonder aantoonbare reden.
    """
    kapot = templates_lotc.env.from_string(
        '{% set js = "f(" ~ (naam | tojson) ~ ")" %}<c-button label="x" @click="{{ js }}" />'
    ).render(naam="project")
    heel = templates_lotc.env.from_string(
        '{% set js = ("f(" ~ (naam | tojson) ~ ")") | forceescape %}<c-button label="x" @click="{{ js }}" />'
    ).render(naam="project")

    verouderd = "de componentlaag escapet Markup nu zelf; dan kan forceescape uit de sjablonen en deze regel weg"
    assert 'onclick="f("project")"' in kapot, verouderd
    assert 'onclick="f(&#34;project&#34;)"' in heel
