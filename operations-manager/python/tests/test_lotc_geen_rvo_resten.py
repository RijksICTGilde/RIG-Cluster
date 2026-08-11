"""Bewaakt dat de rvo-resten niet terugkomen in de LOTC-templates.

De LOTC-omgeving laadt drie design systems::

    DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]   # opi/core/templates_lotc.py

``lotc_rvo`` staat wel geinstalleerd en definieert 7568 verschillende ``.rvo-``-selectors,
maar staat niet in die lijst en wordt dus niet geladen. Van de wel geladen pakketten
definieert alleen ``lotc-forms`` er drie, en die gebruiken wij nergens. Een ``rvo-``-klasse
in een LOTC-template wordt daardoor meegestuurd naar de browser en doet daar niets.

Dat is niet de hele waarheid, en dat is precies waarom deze test twee kanten op meet. De
LOTC-pagina's laden ook onze EIGEN stylesheets (wizard.css, metrics-explorer.css, ...), en
die maken een handvol rvo-klassen wel degelijk op; een ervan hangt aan wizard.js. Die zijn
bij het opruimen hernoemd naar ``lotc-``, met dezelfde regel ernaast in dezelfde stylesheet
zodat de roos-bouwlijn zijn opmaak houdt. Wie er een terugzet onder de oude naam, zet een
klasse terug die niets doet.

De uitzonderingen hieronder zijn geen ontheffing maar een AFTELLIJST. Ze horen te
verdwijnen naarmate de fases landen, en de test faalt ook als een uitzondering overbodig
is geworden: een getal dat te hoog staat is net zo goed een fout als een getal dat
overschreden wordt.

Commentaar valt erbuiten. Dat de herkomst van een blok uitgelegd wordt met het woord
``rvo-`` is nuttig en hoort te mogen blijven staan; het gaat om attribuutwaarden en
CSS-verwijzingen, niet om elk voorkomen van vier letters.
"""

import re
from pathlib import Path

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"

#: Jinja-commentaar. Wordt weggeknipt voor het tellen; zie de moduledocstring.
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
CLASS_ATTR = re.compile(r'class="([^"]*)"')
VAR_RVO = re.compile(r"var\(\s*--rvo-")

#: Wat fase 4 nog niet gedaan heeft: 158 klassen in de pagina die het LOTC-plan apart
#: beoordeelt. Er 158 klassen uit poetsen vooruitlopend op een mogelijke vervanging is
#: verspilde moeite, dus die staat hier tot dat besluit valt.
ARCHITECTURE_OVERVIEW = "architecture-overview.html.j2"

#: Bestanden die hun rvo-klassen nog dragen, met het aantal. Ze stonden hier als
#: "vanaf geen enkele route bereikbaar": zolang roos bestond rendeerde de wizard zijn
#: TemplatePartials en zijn bijlagenfragmenten uit de roos-boom, en waren deze
#: LOTC-kopieen dode letter.
#:
#: Sinds RC-67 is dat niet meer waar: de roos-boom is weg, dus de formulierlaag en de
#: routes renderen DEZE bestanden. De klassen doen er nog steeds niets (het thema laadt
#: ze niet), dus het blijft cosmetische rest en geen storing - maar het is nu opruimwerk
#: aan iets wat WEL gerenderd wordt. Zie features/lotc-rvo-opruiming.md.
#:
#: Deze test houdt de lijst vast: het aantal mag alleen omlaag.
NOG_MET_KLASSEN = {
    "example.html.j2": 2,
    "formulier-template.html.j2": 13,
    "invite-error.html.j2": 5,
    "invite-landing.html.j2": 8,
    "invite-register.html.j2": 2,
    "invite-success.html.j2": 3,
    "partials/deployment_metrics.html.j2": 3,
    "project-creation-error.html.j2": 4,
    "project-creation-partial.html.j2": 4,
    "project-creation-result.html.j2": 3,
    "project-creation-success.html.j2": 5,
    "project-details/_argocd-deployment-card.html.j2": 27,
    "project-details/_resource-usage.html.j2": 12,
    "tools.html.j2": 8,
    "wizard/modal_wizard_review.html.j2": 1,
    "wizard/partials/approval_items.html.j2": 13,
    "wizard/partials/attachments_list.html.j2": 7,
    "wizard/partials/attachments_upload.html.j2": 3,
    "wizard/partials/backup_select_deployment.html.j2": 8,
    "wizard/partials/deployment_info.html.j2": 3,
    "wizard/partials/domain_info.html.j2": 7,
    "wizard/partials/restore_select_backup.html.j2": 4,
    "wizard/partials/restore_select_target.html.j2": 9,
}

#: Zelfde opzet voor de ontwerpvariabelen. Die zijn geen dode letter maar een LEVENDE
#: verwijzing: het shimblok onderaan static/css/lotc-app.css vult ze in. Wie er een
#: weghaalt zonder vervanging, verandert een kleur of een afstand.
KLASSEN_UITZONDERINGEN = {ARCHITECTURE_OVERVIEW: 158, **NOG_MET_KLASSEN}
VARIABELEN_UITZONDERINGEN = {
    ARCHITECTURE_OVERVIEW: 81,
    "wizard/partials/backup_select_deployment.html.j2": 4,
}


def _tel(patroon_teller) -> dict[str, int]:
    """Tel per template, met het commentaar eruit geknipt."""
    gevonden: dict[str, int] = {}
    for path in sorted(TEMPLATES_LOTC_DIR.rglob("*.j2")):
        aantal = patroon_teller(JINJA_COMMENT.sub("", path.read_text()))
        if aantal:
            gevonden[str(path.relative_to(TEMPLATES_LOTC_DIR))] = aantal
    return gevonden


def _tel_klassen(tekst: str) -> int:
    return sum(1 for m in CLASS_ATTR.finditer(tekst) for token in m.group(1).split() if token.startswith("rvo-"))


def _vergelijk(gevonden: dict[str, int], verwacht: dict[str, int], wat: str) -> None:
    nieuw = {n: a for n, a in gevonden.items() if n not in verwacht}
    gegroeid = {n: (verwacht[n], a) for n, a in gevonden.items() if n in verwacht and a > verwacht[n]}
    overbodig = {n: (a, gevonden.get(n, 0)) for n, a in verwacht.items() if gevonden.get(n, 0) < a}
    assert not nieuw, f"nieuwe {wat} in templates_lotc: {nieuw}"
    assert not gegroeid, f"meer {wat} dan vastgelegd (was, is): {gegroeid}"
    assert not overbodig, f"minder {wat} dan vastgelegd - werk de lijst in deze test bij (was, is): {overbodig}"


def test_geen_nieuwe_rvo_klassen_in_de_lotc_templates() -> None:
    """Geen rvo-klasse in een class-attribuut, buiten de aftellijst."""
    _vergelijk(_tel(_tel_klassen), KLASSEN_UITZONDERINGEN, "rvo-klassen")


def test_geen_nieuwe_rvo_variabelen_in_de_lotc_templates() -> None:
    """Geen var(--rvo-...) in een style-attribuut of style-blok, buiten de aftellijst."""
    _vergelijk(_tel(lambda t: len(VAR_RVO.findall(t))), VARIABELEN_UITZONDERINGEN, "var(--rvo-)-verwijzingen")


def test_de_bereikbare_templates_zijn_schoon() -> None:
    """De kern van fase 1: buiten de aftellijst staat er geen enkele rvo-rest meer.

    Deze staat er los bij omdat hij de BELOFTE uitspreekt in plaats van de boekhouding.
    Als de twee tests hierboven groen zijn maar deze rood, is de aftellijst gegroeid
    zonder dat iemand het merkte.
    """
    resten = {
        naam: aantal
        for naam, aantal in {**_tel(_tel_klassen), **_tel(lambda t: len(VAR_RVO.findall(t)))}.items()
        if naam not in KLASSEN_UITZONDERINGEN and naam not in VARIABELEN_UITZONDERINGEN
    }
    assert not resten, f"rvo-resten buiten de aftellijst: {resten}"
