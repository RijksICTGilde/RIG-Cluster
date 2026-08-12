"""Knoppen op één maat, en varianten die bestaan.

WAT ER MISGING

Twee dingen, met dezelfde oorzaak: het component slikt een woord dat het niet kent
zonder een kik te geven.

* De MAAT. In de sjablonen stonden ``sm``, ``md``, ``xs`` en honderd knoppen die niets
  zeiden. Naast elkaar staande knoppen waren daardoor niet even hoog, en dezelfde soort
  actie zag er op twee pagina's anders uit.
* De VARIANT. ``type="submit"`` stond op negen knoppen. ``submit`` is geen variant (dat
  zijn primary, secondary, tertiary, quaternary, subtle, warning, warning-subtle); het
  HTML-type heet in LOTC ``html-type``. Die knoppen kregen geen enkele stijlklasse EN
  dienden hun formulier niet in - twee fouten van één typefout, allebei zonder melding.

DE REGEL DIE HIERONDER BEWAAKT WORDT

``sm`` voor een knop in een dichte, herhaalde context (tabelrij, kaart in een lijst);
verder niets opschrijven, want ``md`` is de standaard van het component. De reden per
maat staat in ``features/knopmaten.md``, het vocabulaire in ``opi/core/buttons.py``.

WAAROM EEN SJABLOONTEST EN NIET EEN SCHERMTEST

Een screenshot toont een knop; dat hij een halve maat kleiner is dan zijn buurman zie
je pas als je ze naast elkaar legt, en dan nog alleen als je het toevallig opmerkt.
Wat er misgaat staat in het sjabloon, dus daar wordt het gelezen.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from opi.core.buttons import BUTTON_SIZES, BUTTON_VARIANTS, check_button_variant
from opi.core.template_helpers import CATALOG_DIR, TEMPLATES_DIR
from opi.core.templates_lotc import templates_lotc
from opi.services.services import DeploymentAction
from opi.web.project_actions import ProjectAction

KNOP = re.compile(r"<c-button\b[^>]*?>", re.DOTALL)
#: ``type=`` en ``size=``, maar niet ``html-type=`` of ``:size=``.
TYPE_ATTRIBUUT = re.compile(r'(?<![-\w:])type="([^"]*)"')
SIZE_ATTRIBUUT = re.compile(r'(?<![-\w:])size="([^"]*)"')

#: Een ``type`` die uit Python of uit een Jinja-variabele komt, met de plek waar die
#: waarde WEL gecontroleerd wordt. Een nieuwe dynamische variant hoort hier pas thuis
#: als daar een antwoord op is: in het sjabloon kan deze test er niets van lezen.
TYPE_UIT_CODE = {
    "{{ action.kind }}": "DeploymentAction.__post_init__ -> check_button_variant",
    "{{ knoptype }}": "bg/_action-confirm.html.j2 klemt de waarde zelf af",
    "{{ submit.kind|e }}": "opi/forms/layout.py Submit.kind, vaste waarden in de code",
}

#: Sjablonen met een kale ``<button>`` die daar met opzet staat, met de reden. Een knop
#: hoort een ``<c-button>`` te zijn: een kale krijgt geen enkele stijlklasse van het
#: thema. De eerste twee zijn geen paginaknoppen maar onderdelen van een veld, met eigen
#: CSS en eigen JavaScript dat ze op klasse terugvindt.
KALE_BUTTON_TOEGESTAAN = {
    "components/_forms.j2": "het kopieerknopje IN het kopieerveld (lotc-copyfield, forms.css)",
    "widgets/key_value_editor.html.j2": "de ENV/YAML-schakelaar van het veld (kv-toggle, wizard.css)",
    "project-details/_argocd-deployment-card.html.j2": (
        "dood sjabloon; de levende versie (bg/_argocd-deployment-card.html.j2) heeft een <c-button>"
    ),
}


#: Een Jinja-commentaarblok. Wordt weggehaald voor het lezen, met behoud van de
#: regelnummers: in de toelichtingen staat uitgelegd waarom ergens GEEN kale <button>
#: staat, en dat woord mag geen bevinding worden.
COMMENTAAR = re.compile(r"\{#.*?#\}", re.DOTALL)

#: Een tekstwaarde tussen aanhalingstekens IN een Jinja-uitdrukking.
LITERAAL = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _templatebestanden() -> list[pathlib.Path]:
    """Alle Jinja-sjablonen van het portaal: de eigen map plus die van de diensten."""
    bestanden: list[pathlib.Path] = []
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        bestanden.extend(pathlib.Path(map_).rglob("*.j2"))
    return bestanden


def _zonder_commentaar(tekst: str) -> str:
    """De sjabloontekst met de toelichtingen eruit, regelnummers intact."""
    return COMMENTAAR.sub(lambda m: "\n" * m.group(0).count("\n"), tekst)


def _knoppen() -> list[tuple[str, int, str]]:
    """Elke ``<c-button>`` in de sjablonen als (pad, regelnummer, tag)."""
    gevonden: list[tuple[str, int, str]] = []
    for pad in _templatebestanden():
        tekst = _zonder_commentaar(pad.read_text())
        for treffer in KNOP.finditer(tekst):
            regel = tekst[: treffer.start()].count("\n") + 1
            gevonden.append((str(pad), regel, treffer.group(0)))
    return gevonden


def test_er_zijn_knoppen_om_te_bewaken() -> None:
    """Zonder deze regel is een test die niets vindt niet van een groene te onderscheiden."""
    assert len(_knoppen()) > 100


def test_elke_knopmaat_staat_in_de_regel() -> None:
    """Alleen ``sm`` wordt opgeschreven; de rest is de standaard en zwijgt."""
    buiten = [
        f"{pad}:{regel} size={maat.group(1)!r}"
        for pad, regel, tag in _knoppen()
        if (maat := SIZE_ATTRIBUUT.search(tag)) and maat.group(1) not in BUTTON_SIZES
    ]
    assert buiten == [], (
        "Deze knoppen dragen een maat die niet in de regel staat. Toegestaan is "
        f"{sorted(BUTTON_SIZES)} voor een knop in een dichte, herhaalde context; alles "
        "daarbuiten laat size weg en krijgt de standaardmaat. Zie features/knopmaten.md:\n  " + "\n  ".join(buiten)
    )


def test_geen_knopmaat_die_van_buiten_komt() -> None:
    """Een maat uit een variabele is een maat die deze bewaker niet kan lezen.

    En daarmee een maat die buiten de regel kan vallen zonder dat iemand het merkt: het
    component neemt elke waarde aan, of hij nu bestaat of niet.
    """
    dynamisch = [
        f"{pad}:{regel} size={maat.group(1)!r}"
        for pad, regel, tag in _knoppen()
        if (maat := SIZE_ATTRIBUUT.search(tag)) and "{{" in maat.group(1)
    ]
    assert dynamisch == [], "Schrijf de maat voluit of laat hem weg:\n  " + "\n  ".join(dynamisch)


def test_elke_knopvariant_bestaat() -> None:
    """Een ``type`` die het component niet kent levert een knop zonder stijl op."""
    onbekend = []
    for pad, regel, tag in _knoppen():
        gevonden = TYPE_ATTRIBUUT.search(tag)
        if not gevonden:
            continue
        waarde = gevonden.group(1)
        if "{{" in waarde:
            # Een uitdrukking die zelf zijn varianten opschrijft ("primary als ..., anders
            # secondary") is wel te lezen: dan zijn die woorden de waarden.
            literalen = [g for treffer in LITERAAL.finditer(waarde) for g in treffer.groups() if g]
            if literalen:
                onbekend.extend(
                    f"{pad}:{regel} type={literaal!r} (in een uitdrukking)"
                    for literaal in literalen
                    if literaal not in BUTTON_VARIANTS
                )
            elif waarde not in TYPE_UIT_CODE:
                onbekend.append(f"{pad}:{regel} type={waarde!r} (niet te lezen, en nergens afgeklemd)")
            continue
        if waarde not in BUTTON_VARIANTS:
            onbekend.append(f"{pad}:{regel} type={waarde!r}")
    assert onbekend == [], (
        f"Deze knoppen dragen een type dat geen variant is. Bestaande varianten: {sorted(BUTTON_VARIANTS)}. "
        'Bedoelde je het HTML-type? Dat heet html-type ("submit"), niet type:\n  ' + "\n  ".join(onbekend)
    )


def test_geen_kale_button_waar_een_c_button_hoort() -> None:
    """Een kale ``<button>`` krijgt geen enkele klasse van het thema."""
    kaal = []
    for pad in _templatebestanden():
        if any(str(pad).endswith(sleutel) for sleutel in KALE_BUTTON_TOEGESTAAN):
            continue
        for nr, regel in enumerate(_zonder_commentaar(pad.read_text()).splitlines(), start=1):
            if re.search(r"<button[\s>]", regel):
                kaal.append(f"{pad}:{nr}")
    assert kaal == [], (
        "Deze sjablonen zetten een kale <button> neer. Schrijf <c-button>; een onclick gaat mee via "
        ':attrs, en een formulier dien je in met html-type="submit":\n  ' + "\n  ".join(kaal)
    )


# --- waarom de bewaker nodig is: het component klaagt niet ------------------------


def test_een_onbekende_variant_gaat_stil_door_naar_het_element() -> None:
    """De keerzijde. Zonder deze helft is niet te zien wat er misgaat.

    Onder NLDD wordt onze ``type`` de ``variant`` van het element, via een tabel die een
    woord dat er niet in staat ONGEWIJZIGD doorgeeft. ``type="submit"`` levert dus
    ``variant="submit"``: een variant die geen enkel stijlblad kent. En omdat het echte
    HTML-type ``html-type`` heet, blijft dat ``button`` staan - de knop dient zijn
    formulier niet eens in. Twee fouten, geen melding.
    """
    kapot = templates_lotc.env.from_string('<c-button type="submit" label="Opslaan" />').render()
    heel = templates_lotc.env.from_string('<c-button html-type="submit" type="primary" label="Opslaan" />').render()

    assert 'variant="submit"' in kapot
    assert 'type="button"' in kapot
    assert 'variant="primary"' in heel
    assert 'type="submit"' in heel


def test_de_standaardmaat_is_md_en_die_schrijf_je_dus_niet_op() -> None:
    """Waarom de regel "alleen sm opschrijven" geen maat weggooit.

    Een knop zonder ``size`` krijgt ``md`` van het component. ``size="md"`` erbij
    schrijven levert exact hetzelfde element op, en daarmee twee manieren om hetzelfde
    op te schrijven -- precies waar de maten uit elkaar zijn gaan lopen.
    """
    zonder = templates_lotc.env.from_string('<c-button label="x" />').render()
    met = templates_lotc.env.from_string('<c-button size="md" label="x" />').render()
    dicht = templates_lotc.env.from_string('<c-button size="sm" label="x" />').render()

    assert 'size="md"' in zonder
    assert zonder == met
    assert 'size="sm"' in dicht


# --- de varianten die uit Python komen -------------------------------------------


def test_een_actie_met_een_onbekende_variant_struikelt() -> None:
    """``action.kind`` wordt rechtstreeks een ``type``; daar leest geen enkele test aan."""
    with pytest.raises(ValueError, match="bestaat niet"):
        DeploymentAction(label="Wakker maken", icon="play", kind="danger", endpoint="/x")

    with pytest.raises(ValueError, match="bestaat niet"):
        ProjectAction(key="delete", label="Verwijderen", icon="trash", kind="danger", endpoint="/x", message="?")


def test_een_actie_met_een_bestaande_variant_komt_er_door() -> None:
    actie = DeploymentAction(label="Wakker maken", icon="play", kind="secondary", endpoint="/x")
    assert actie.kind == "secondary"
    check_button_variant("warning-subtle", "test")
