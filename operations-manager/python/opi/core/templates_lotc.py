"""Tweede templateomgeving, op Lord of the Components in plaats van jinja-roos.

Dit is de POC-bouwlijn naast de release. Waarom een TWEEDE omgeving en niet een
extra design system in de bestaande: beide systemen registreren een Jinja-extensie
die de bron voorbewerkt en elke ``<c-*>``-tag opeist. Zet je ze in dezelfde
``Environment``, dan claimt de eerst geregistreerde voorbewerker alle tags en breekt
hard op de tags die hij niet kent. Er is geen doorlaatstand aan beide kanten. Gemeten
in ``docs/lotc-samenleven-met-jinja-roos.md``.

Twee losse omgevingen werken wel. De grens loopt daarbij niet per pagina maar per
overervingsketen: een template dat ``base_lotc.html.j2`` uitbreidt wordt door deze
omgeving gerenderd, een template dat ``base.html.j2`` uitbreidt door de roos-omgeving.

De activeringsvolgorde van de design systems ligt vast: ``lotc-forms`` moet als
laatste, na het visuele thema, anders lossen de invoervelden niet op.
"""

import logging
from pathlib import Path
from typing import Any

import markupsafe
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemLoader, TemplateNotFound
from lord_of_the_components import get_static_roots, setup_components
from markupsafe import Markup

from opi.core.config import BUILD_DATE, VERSION
from opi.core.templates import (
    deployment_action_key,
    format_dutch_date,
    format_rrule_schedule,
    get_service_definition_for_entry,
    get_service_name,
    get_version_info,
    static_url,
)
from opi.forms.lotc_attrs import field_attrs
from opi.forms.widgets.roos import _attr_escape as attr_escape

logger = logging.getLogger(__name__)


def _to_nldd_icon(naam: str) -> str:
    """Vertaal een ROOS-iconnaam naar de NLDD-woordenschat.

    De import staat binnenin om dezelfde kringloop te vermijden als elders in dit bestand:
    navigation_lotc leunt op de menu-opbouw, en die komt via de routes hier langs.
    """
    from opi.web.navigation_lotc import to_nldd_icon

    return to_nldd_icon(naam)

# lotc-forms hoort achteraan; zie de moduledocstring.
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent / "templates_lotc"

# Losse Jinja2Templates, met een eigen loader. setup_components hangt de LOTC-
# componenttemplates aan loader.searchpath, dus dit moet een FileSystemLoader blijven.
templates_lotc = Jinja2Templates(directory=str(TEMPLATES_LOTC_DIR))
if not isinstance(templates_lotc.env.loader, FileSystemLoader):
    raise TypeError("templates_lotc env loader must be a FileSystemLoader for the LOTC search path")

# autoescape is verplicht: de renderers escapen propwaarden en behandelen inhoud als
# al-veilige Markup. Met autoescape uit zou gebruikersdata niet geescaped worden.
templates_lotc.env.autoescape = True
setup_components(templates_lotc.env, design_systems=DESIGN_SYSTEMS, htmx=True)

# Hier stond een eigen c-secret-field, omdat LOTC die nog niet had. Sinds 8192d6a levert
# het LOTC-project hem zelf, en de opruiming was precies wat we ervan hoopten: dit blok
# weg, het template weg, geen enkele paginawijziging - de aanroepvorm was al die van hen.
#
# De weg om zoiets opnieuw te doen staat beschreven in features/lotc-bouwlijn.md:
# merge_fragment op de registry plus de eigen templatemap op de searchpath.

# Dezelfde globals en filters als de roos-omgeving, zodat een omgezet template niet
# ook nog zijn aanroepen naar version_info(), static_url() en de filters hoeft te
# veranderen. De omzetting gaat over componenten, niet over de rest van het template.
templates_lotc.env.globals["version"] = VERSION
templates_lotc.env.globals["build_date"] = BUILD_DATE
templates_lotc.env.globals["version_info"] = get_version_info
templates_lotc.env.globals["static_url"] = static_url
# De attribuutbundel van een formulierveld, voor LOTC's :attrs-spread. Vervangt de
# macro's die in de roos-templates attribuut-TEKST in de tag schreven; zie
# opi/forms/lotc_attrs.py voor waarom dat bij LOTC niet kan.
templates_lotc.env.globals["field_attrs"] = field_attrs


def render_roos(name: str, **context: Any) -> Markup:
    """Render een template uit de ROOS-omgeving en zet het resultaat hier neer.

    Dit is de uitweg voor blokken die een DIENST meelevert: die sjablonen staan in
    opi/services/catalog/ en zijn in roos-componenten geschreven. Ze renderen niet in deze
    omgeving - de map staat niet op dit zoekpad, en twee componentsystemen kunnen niet in
    een Jinja-omgeving, want de eerst geregistreerde voorbewerker eist elke <c-*>-tag op.

    De afweging: zo'n blok in de nieuwe vormgeving NAmaken betekent een tweede kopie die
    uit de pas gaat lopen zodra een dienst zijn eigen sjabloon wijzigt - en diensten zijn
    juist het deel van dit platform dat blijft groeien. Het blok WEGLATEN is erger: dan
    verdwijnt functionaliteit zonder dat iemand het merkt, en dat is precies wat deze
    omzetting niet mag doen.

    Dus rendert het blok met zijn eigen omgeving en komt het als HTML binnen. Dat is
    eerlijk zichtbaar: zo'n blok draagt rvo-klassen en ziet er anders uit dan de rest van
    de pagina, totdat de dienst zelf meegaat. Zichtbaar anders is beter dan stilletjes weg.

    Een sjabloon dat niet bestaat wordt overgeslagen met een melding in het log, niet met
    een foutpagina: een dienst die een blok aankondigt dat er niet is, mag de projectpagina
    niet meenemen in zijn val.
    """
    from opi.core.templates import get_templates

    try:
        template = get_templates().env.get_template(name)
    except TemplateNotFound:
        logger.warning("Dienstblok overgeslagen, sjabloon niet gevonden: %s", name)
        return Markup("")

    # Markup() zegt: dit is HTML, escape het niet nog een keer. Dat is hier veilig en
    # noodzakelijk, en het is de moeite waard om te zeggen WAAROM, want een verkeerde
    # Markup() is hoe cross-site scripting binnenkomt.
    #
    # De naam komt niet van een gebruiker maar uit de dienstenregistry - een vaste lijst in
    # de code - en het sjabloon zelf is van ons. De GEGEVENS erin worden door de
    # roos-omgeving geescaped, want die staat op autoescape: een projectnaam met
    # <script> erin komt er als tekst uit. Wat hier binnenkomt is dus al veilige HTML, en
    # zonder Markup() zou je die HTML letterlijk op het scherm zien staan.
    #
    # Wat hier NIET mag gebeuren, en de reden dat dit een aparte functie is: gerenderde
    # HTML nog een keer door een sjabloonmotor halen. Dan wordt {{ }} in een ingevulde
    # waarde alsnog uitgevoerd, en dat is in deze codebase eerder een lek geweest.
    return Markup(template.render(**context))  # noqa: S704


templates_lotc.env.globals["render_roos"] = render_roos

# De widgettemplates delen macro's die een attribuutwaarde escapen (optional_attr,
# bool_attr). Dat filter hoort bij de kale widget-omgeving van de roos-adapter; de
# LOTC-adapter rendert dezelfde macro's in DEZE omgeving, dus hier hoort het ook te
# staan. Zonder valt de formulierlaag om op elk veld dat de macro's gebruikt.
templates_lotc.env.filters["attr_escape"] = attr_escape

# Onze ROOS-iconnamen naar de NLDD-woordenschat. Als FILTER en niet vooraf in de data,
# omdat dezelfde gegevens ook de oude weergave voeden: die verwacht juist de ROOS-naam.
# Zonder deze vertaling rendert een icoon leeg - stil, want een onbekende naam levert geen
# fout op. Zo misten de dienstkaarten in de wizard hun iconen, op PostgreSQL na: "database"
# heet toevallig in beide woordenschatten hetzelfde.
templates_lotc.env.filters["nldd_icon"] = _to_nldd_icon
templates_lotc.env.filters["service_name"] = get_service_name
templates_lotc.env.filters["service_definition"] = get_service_definition_for_entry
templates_lotc.env.filters["dutch_date"] = format_dutch_date
templates_lotc.env.filters["rrule_schedule"] = format_rrule_schedule
templates_lotc.env.filters["deployment_action_key"] = deployment_action_key

templates_lotc.env.add_extension("jinja2.ext.i18n")

# De filesystemwortels die de /static/lotc/-URL-ruimte vullen: de kernstijlen plus de
# bundel van elk geactiveerd design system. Die liggen in verschillende geinstalleerde
# pakketten, dus de route hieronder probeert ze op volgorde.
LOTC_STATIC_ROOTS = [Path(root).resolve() for root in get_static_roots()]


def resolve_lotc_static(rel: str) -> Path | None:
    """Zoek ``rel`` op onder de LOTC-staticwortels; None als het er niet is.

    Een verzoek om ``/static/lotc/<rest>`` valt op ``<wortel>/lotc/<rest>``, eerste
    treffer wint. De ``is_relative_to``-controle houdt padtraversal buiten de deur:
    ``rel`` komt uit de URL en mag niet buiten zijn wortel wijzen.
    """
    for root in LOTC_STATIC_ROOTS:
        candidate = (root / "lotc" / rel).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    return None


def process_components_lotc(html: object) -> markupsafe.Markup:
    """Render componenttags in HTML die pas tijdens de aanvraag ontstaat.

    De voorbewerker van de extensie draait alleen bij het compileren van een template,
    dus een string die tijdens de aanvraag wordt samengesteld (bijvoorbeeld formulier-
    HTML uit de editables) moet er alsnog langs.

    De waarde wordt eerst naar tekst gebracht. Dat is niet voor de vorm: een template
    dat dit filter op een niet-gezette variabele toepast levert een Undefined, en die
    rechtstreeks aan from_string geven geeft een TypeError uit de Jinja-compiler in
    plaats van de lege uitvoer die overal elders de gewoonte is.
    """
    rendered = templates_lotc.env.from_string(str(html)).render()
    return markupsafe.Markup(rendered)  # noqa: S704


templates_lotc.env.filters["process_components"] = process_components_lotc
