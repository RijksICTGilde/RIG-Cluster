"""Geen kleur die op een ONBEKENDE ontwerpvariabele terugvalt.

DE FOUT DIE DIT TEGENHOUDT (RC-134). In de donkere weergave stond op meerdere schermen
tekst die je niet kon lezen. De oorzaak was elke keer dezelfde vorm:

    background: var(--nldd-color-surface, #fff);

Een naam die NERGENS gezet wordt, met een vaste lichte kleur als terugval. De browser
neemt dan altijd die terugval, want de variabele bestaat niet - en een vaste kleur beweegt
per definitie niet mee met de licht/donker-stand, terwijl de tekst eroverheen dat wel doet.
Gemeten op de projectpagina, blok "Configuratie & Secrets": #FFFFFF op #FFFFFF, contrast
1,00; de projectnaam en beide AGE-sleutels waren letterlijk onzichtbaar.

Dit is geen verbod op vaste kleuren. Een vaste kleur ACHTER een token dat wel bestaat is
prima: die terugval wordt nooit gebruikt zolang het thema geladen is, en is er juist voor
het geval dat niet zo is. Wat hier rood wordt is de combinatie "onbekende naam + vaste
kleur", want dan is de terugval de waarde en niet het vangnet.

WAAROM DIT EEN BRONTEST IS EN NIET ALLEEN EEN BROWSERMETING.
tests/e2e/test_donkere_weergave_contrast.py meet het contrast op de schermen die de
opdrachtgever noemde; dat is de meting die de reparatie vaststelde. Maar zo'n meting dekt
alleen wat op DIE schermen staat. Een nieuwe regel met dezelfde vorm op een scherm dat de
suite niet opent, blijft daar onzichtbaar. Deze test leest de bron en dekt alles.

WAT ER GESCAND WORDT: onze eigen stijlbladen en de <style>-blokken in onze sjablonen, plus
de handgeschreven componenten uit de componentenlaag - die laatste omdat wij hun onbekende
namen in lotc-app.css invullen, en deze test dus zegt wanneer daar een naam bij komt.

WAAROM EEN KALE ``#333`` HIER NIET ROOD WORDT. Het plan vroeg een poort op "vaste
hexkleuren OF vaste toonnamen", met de uitweg "kan dat niet zonder valse alarmen, zeg dan
waarom". Dit is die waarom. Een kale hex is in onze eigen CSS massaal legitiem: hij staat
in ``box-shadow``, ``outline``, ``border`` van een decoratief vlak, in een ``gradient`` en
in een ``data:``-URI van een pijltje - allemaal plekken waar hij niets onleesbaar maakt.
Een poort daarop zou vrijwel alleen valse alarmen geven en dus binnen een week uitgezet
worden. De vorm die hierboven staat is daarentegen ALTIJD fout: een naam die niemand zet
kan per definitie niet meebewegen, dus is de terugval de werkelijke waarde.

Wat er daardoor doorheen glipt, en waar de browsermeting voor is: een regel die voorgrond
EN achtergrond allebei vast zet, zonder ook maar een ``var()``. Concreet geval in de
componentenlaag: ``.lotc-statusbar--neutral`` t/m ``--error`` in app-components.css zetten
vijf paren vaste lichte kleuren. Die zijn in zichzelf leesbaar (donkere tekst op een licht
vlak, in beide standen dezelfde verhouding), maar staan in een donkere pagina als licht
vlak. Precies dat meet tests/e2e/test_donkere_weergave_contrast.py met zijn aparte
"licht eiland"-regel - dat is daar de poort, niet hier.
"""

from __future__ import annotations

import re
from pathlib import Path

import lotc_nldd
import opi

PYTHON_DIR = Path(opi.__file__).parent.parent
STATIC_CSS = PYTHON_DIR / "static" / "css"
SJABLONEN = Path(opi.__file__).parent / "templates_lotc"
#: De componentenlaag staat naast lotc_nldd in site-packages.
SITE_PACKAGES = Path(next(iter(lotc_nldd.__path__))).parent

#: ``var(--naam, terugval)`` - de terugval is alles tot de sluitende haak van deze var().
_VAR_MET_TERUGVAL = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*,\s*([^;{}]+?)\s*\)(?=[;\s}),])")

#: Een definitie: ``--naam:`` aan het begin van een declaratie.
_DEFINITIE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")

#: Terugvalwaarden die een KLEUR zijn. Een terugval van 8px of ``inherit`` gaat deze test
#: niet aan: die maakt niets onleesbaar.
_KLEUR = re.compile(
    r"^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|oklch\(|oklab\(|lab\(|lch\(|color\("
    r"|white|black|red|green|blue|grey|gray|silver|whitesmoke|transparent)",
    re.IGNORECASE,
)


def _themabestanden() -> list[Path]:
    """De stylesheets waarin het NLDD-thema zijn tokens zet."""
    return sorted((SITE_PACKAGES / "lotc_nldd" / "static" / "lotc" / "nldd" / "dist" / "css").glob("*.css"))


def _onze_bestanden() -> list[Path]:
    return sorted(STATIC_CSS.glob("*.css"))


def _componentbestanden() -> list[Path]:
    """De handgeschreven componenten: hun opmaak staat in het sjabloon of in een .css."""
    paden: list[Path] = []
    for pakket in ("lord_of_the_components", "lotc_nldd", "lotc_forms", "lotc_layout"):
        wortel = SITE_PACKAGES / pakket
        if not wortel.is_dir():
            continue
        paden.extend(sorted(wortel.rglob("*.html.j2")))
        paden.extend(sorted(p for p in wortel.rglob("*.css") if "nldd/dist/css" not in p.as_posix()))
    return paden


def _bekende_namen() -> set[str]:
    """Elke ontwerpvariabele die ergens GEZET wordt: door het thema, of door ons."""
    bekend: set[str] = set()
    for pad in [*_themabestanden(), *_onze_bestanden()]:
        bekend.update(_DEFINITIE.findall(pad.read_text()))
    for pad in SJABLONEN.rglob("*.j2"):
        bekend.update(_DEFINITIE.findall(pad.read_text()))
    # Een component mag zijn eigen custom properties zetten; die staan in zijn eigen bron.
    # RANDJE: daardoor telt ook een component dat ZELF ``--nldd-color-surface: #fff`` zet
    # als "bekend", terwijl de fout dan dezelfde is - de naam bestaat wel, maar met een
    # vaste lichte waarde. Nu doet geen enkel component dat; komt het voor, dan zwijgt deze
    # poort erover en moet de browsermeting het vangen.
    for pad in _componentbestanden():
        bekend.update(_DEFINITIE.findall(pad.read_text()))
    return bekend


def _terugvallen_op_onbekende_namen(paden: list[Path], bekend: set[str]) -> list[str]:
    treffers: list[str] = []
    for pad in paden:
        for nr, regel in enumerate(pad.read_text().splitlines(), 1):
            for naam, terugval in _VAR_MET_TERUGVAL.findall(regel):
                if naam in bekend or not _KLEUR.match(terugval.strip()):
                    continue
                treffers.append(f"{pad.name}:{nr}  var({naam}, {terugval.strip()})")
    return treffers


def test_onze_eigen_opmaak_valt_niet_terug_op_een_onbekende_kleurvariabele() -> None:
    """Onze stijlbladen en de <style>-blokken in onze sjablonen."""
    bekend = _bekende_namen()
    paden = [*_onze_bestanden(), *sorted(SJABLONEN.rglob("*.j2"))]

    treffers = _terugvallen_op_onbekende_namen(paden, bekend)

    assert treffers == [], (
        "Deze regels vragen een ontwerpvariabele op die nergens gezet wordt, met een vaste "
        "kleur als terugval. De browser neemt dan altijd die vaste kleur, en die beweegt "
        "niet mee met de licht/donker-stand - dat is precies hoe RC-134 onleesbare tekst "
        "opleverde. Zet de naam in static/css/lotc-app.css uit een themawaarde, of gebruik "
        "het themotoken rechtstreeks:\n  " + "\n  ".join(treffers)
    )


#: De namen die de componentenlaag opvraagt zonder dat iemand ze zet, en die wij BEWUST
#: niet invullen - met de reden erbij. Ze staan alle drie op een vlak dat het component
#: ZELF ook vastzet, dus voorgrond en achtergrond horen bij elkaar en zijn in beide standen
#: even leesbaar; er alsnog een themawaarde achter zetten zou een van de twee kanten laten
#: meebewegen en juist het contrast slopen.
#:
#:   --semantics-action-primary-background-color   .lotc-avatar: wit op een vast #154273
#:   --semantics-feedback-warning-color            .lotc-unimplemented (vaste donkere tekst
#:                                                 op een vast gestreept lichtvlak) en de
#:                                                 border-left van .lotc-action--warning
#:   --semantics-feedback-error-color              border-left van .lotc-action--critical:
#:                                                 een randkleur, geen tekst op een vlak
BEWUST_NIET_INGEVULD = {
    "--semantics-action-primary-background-color",
    "--semantics-feedback-warning-color",
    "--semantics-feedback-error-color",
}


def test_de_componentenlaag_vraagt_geen_kleurnaam_die_wij_niet_invullen() -> None:
    """De handgeschreven componenten uit de componentenlaag.

    Zij schrijven ``var(--nldd-color-surface, #fff)`` en dergelijke; wij vullen die namen
    in static/css/lotc-app.css in uit het thema. Komt er bij een nieuwe versie van de
    componentenlaag een naam bij, dan valt die stil terug op zijn vaste lichte kleur en is
    de donkere weergave daar weer stuk. Deze test zegt dan WELKE naam erbij kwam - en
    dwingt af dat er dan een keuze gemaakt wordt: invullen, of hier met de reden erbij.
    """
    bekend = _bekende_namen() | BEWUST_NIET_INGEVULD

    treffers = _terugvallen_op_onbekende_namen(_componentbestanden(), bekend)

    assert treffers == [], (
        "De componentenlaag vraagt kleurvariabelen op die noch het thema noch wij zetten, "
        "dus wint hun vaste lichte terugval en is dat component in de donkere weergave "
        "onleesbaar. Vul de naam in static/css/lotc-app.css in uit een themawaarde en meld "
        "het in request_for_components.md - of zet hem in BEWUST_NIET_INGEVULD met de "
        "meting die zegt waarom hij zo mag blijven:\n  " + "\n  ".join(treffers)
    )


def test_de_bewust_niet_ingevulde_namen_worden_ook_echt_nog_gevraagd() -> None:
    """Geen dode uitzonderingen: een naam die niemand meer vraagt, hoort hier weg.

    Zonder deze kant blijft ``BEWUST_NIET_INGEVULD`` groeien met namen uit componenten die
    allang anders geschreven zijn, en dekt de uitzondering stilletjes iets af wat er niet
    meer is.
    """
    gevraagd = {naam for pad in _componentbestanden() for naam, _ in _VAR_MET_TERUGVAL.findall(pad.read_text())}

    verweesd = sorted(BEWUST_NIET_INGEVULD - gevraagd)

    assert verweesd == [], (
        "Deze namen staan als uitzondering in BEWUST_NIET_INGEVULD, maar geen enkel "
        f"component vraagt ze nog op. Haal ze weg: {verweesd}"
    )
