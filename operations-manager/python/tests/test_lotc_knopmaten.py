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
LABEL_ATTRIBUUT = re.compile(r'(?<![-\w:])label="([^"]*)"')
EXTENDS = re.compile(r"\{%-?\s*extends\s")
FOR = re.compile(r"\{%-?\s*for\s")
ENDFOR = re.compile(r"\{%-?\s*endfor\s*-?%\}")

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
}


#: Knoppen op een PAGINA die ``sm`` dragen terwijl ze niet in een lus staan, en waarom
#: dat daar tóch klopt. De sleutel is (sjabloon, label): een regelnummer verschuift bij
#: de eerste de beste bewerking en dan bewaakt deze lijst iets anders dan wat er staat.
#:
#: Elke regel hier is een belofte dat de knop in een KOPREGEL of een KAART staat en niet
#: paginabreed is. Wordt deze lijst lang, dan is dat het signaal dat de regel zelf niet
#: meer klopt - niet dat er een regel bij moet.
SM_OP_EEN_PAGINA = {
    ("bg/project-tabs.html.j2", "Bewerken"): "in de kopregel van een paneel, naast de titel (panel(aside=...))",
    ("bg/project-tabs.html.j2", "Toevoegen"): "in de kaart Componenten, bij de lijst eronder",
    (
        "bg/project-tabs.html.j2",
        "Deployment toevoegen",
    ): "in de kaart Acties, naast de andere acties van diezelfde kaart",
    (
        "bg/project-tabs.html.j2",
        "Project herverwerken",
    ): "in de kaart Acties, naast Deployment toevoegen; verhuisd uit de Gevarenzone",
    ("bg/admin-users.html.j2", "Gebruiker toevoegen"): "in de kopregel van het paneel, bij de tabel eronder",
    ("bg/admin-approvals.html.j2", "Sluiten"): "de sluitknop in de kopregel van de dialoog",
    ("test-template-variables.html.j2", "Verwijderen"): "ontwikkelpagina die een tabelrijknop naspeelt",
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


def _korte_naam(pad: pathlib.Path) -> str:
    """Het sjabloonpad zoals een include het schrijft: relatief aan zijn eigen map."""
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        try:
            return str(pad.relative_to(map_))
        except ValueError:
            continue
    return str(pad)


def _paginaknoppen_met_een_maat() -> list[tuple[str, int, str, bool]]:
    """Elke ``<c-button>`` MET een maat op een paginasjabloon, en of hij in een lus staat.

    Een paginasjabloon is er een die een layout uitbreidt: dat is het hele scherm, en
    daar staan de hoofdacties. De fragmenten (partials, modals, kaarten) zijn de dichte
    context zelf - die worden per item of binnen een dialoog gerenderd - dus daar zegt
    "staat hij in een lus" niets.
    """
    gevonden: list[tuple[str, int, str, bool]] = []
    for pad in _templatebestanden():
        tekst = _zonder_commentaar(pad.read_text())
        if not EXTENDS.search(tekst):
            continue
        for treffer in KNOP.finditer(tekst):
            if not SIZE_ATTRIBUUT.search(treffer.group(0)):
                continue
            voor = tekst[: treffer.start()]
            in_lus = len(FOR.findall(voor)) > len(ENDFOR.findall(voor))
            gevonden.append((_korte_naam(pad), voor.count("\n") + 1, treffer.group(0), in_lus))
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


def test_er_zijn_paginaknoppen_met_een_maat_om_te_bewaken() -> None:
    """Vindt de lezer hierboven niets, dan is de test hieronder gratis groen."""
    assert len(_paginaknoppen_met_een_maat()) > 10


def test_geen_maat_op_een_knop_die_niet_in_een_dichte_context_staat() -> None:
    """De omgekeerde toets: niet "bestaat deze maat", maar "hoort hij hier".

    Waarom deze kant nodig was. De bewaker hierboven leest of een maat in de regel
    STAAT, en ``sm`` staat erin - dus 'Nieuw Project' en 'Alle projecten' konden op het
    dashboard maandenlang kleiner zijn dan dezelfde knop op elke andere pagina, met alle
    toetsen groen. De gebruiker heeft dat drie keer gemeld.

    Wat wel machinaal te zien is: een knop op een PAGINASJABLOON (een die een layout
    uitbreidt) die NIET in een ``{% for %}`` staat, is geen rij in een tabel en geen
    kaart in een raster. Zo'n knop is verdacht en moet zijn reden opschrijven in
    ``SM_OP_EEN_PAGINA``. Wat een dichte context is weet alleen de plek zelf; deze test
    dwingt af dat die plek het zegt.
    """
    verdacht = []
    for pad, regel, tag, in_lus in _paginaknoppen_met_een_maat():
        if in_lus:
            continue
        label = LABEL_ATTRIBUUT.search(tag)
        naam = label.group(1) if label else ""
        if (pad, naam) in SM_OP_EEN_PAGINA:
            continue
        verdacht.append(f"{pad}:{regel} label={naam!r}")
    assert verdacht == [], (
        "Deze knoppen dragen een maat op een paginasjabloon terwijl ze niet in een lus "
        "staan, en dat is geen dichte, herhaalde context. Haal de size weg (md is de "
        "standaard en die schrijf je niet op), of zet de knop in SM_OP_EEN_PAGINA met de "
        "reden waarom hij daar wel in een kopregel of kaart hoort:\n  " + "\n  ".join(verdacht)
    )


def test_de_omgekeerde_toets_ziet_een_paginabrede_knop_met_een_maat() -> None:
    """Bewaak de bewaker: een knop die niet in de lijst staat MOET rood worden.

    Zonder deze helft is niet te zien of de lezer hierboven echt iets meet. Dit is
    precies de vorm die op het dashboard stond.
    """
    verdacht = []
    for pad, _regel, tag, in_lus in [*_paginaknoppen_met_een_maat(), _DASHBOARDKNOP]:
        if in_lus:
            continue
        label = LABEL_ATTRIBUUT.search(tag)
        naam = label.group(1) if label else ""
        if (pad, naam) not in SM_OP_EEN_PAGINA:
            verdacht.append(naam)
    assert verdacht == ["Nieuw Project"]


#: De knop zoals hij op het dashboard stond, als voer voor de test hierboven.
_DASHBOARDKNOP = (
    "bg/dashboard.html.j2",
    92,
    '<c-button type="primary" size="sm" label="Nieuw Project" icon="plus" href="/forms/wizard/restart" />',
    False,
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
