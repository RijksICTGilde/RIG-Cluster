"""De templateomgeving, op Lord of the Components.

Dit was de tweede omgeving, naast die van jinja-roos-components; sinds RC-67 is het de
enige. Dat er twee moesten zijn zolang beide systemen bestonden, is gemeten in
``docs/lotc-samenleven-met-jinja-roos.md``: allebei registreren ze een Jinja-extensie die
de bron voorbewerkt en elke ``<c-*>``-tag opeist, en in EEN ``Environment`` claimt de
eerst geregistreerde voorbewerker alle tags en breekt hard op de tags die hij niet kent.

De activeringsvolgorde van de design systems ligt vast: ``lotc-forms`` moet als
laatste, na het visuele thema, anders lossen de invoervelden niet op.
"""

import logging
from pathlib import Path

import markupsafe
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemLoader
from lord_of_the_components import get_static_roots, setup_components

from opi.core.config import BUILD_DATE, VERSION
from opi.core.template_helpers import (
    CATALOG_DIR,
    format_dutch_date,
    format_rrule_schedule,
    get_service_definition_for_entry,
    get_service_name,
    static_url,
)
from opi.core.version import get_version_info
from opi.forms.lotc_attrs import attr_escape, bedraad_foutmelding, field_attrs
from opi.services.catalog.aliases.overzicht import alias_variabelen
from opi.services.catalog.aliases.references import is_reference as _alias_is_reference
from opi.services.registry import deployment_action_key

logger = logging.getLogger(__name__)


def _to_nldd_icon(naam: str | None) -> str | None:
    """Vertaal een iconnaam uit de dienstdefinities naar de NLDD-woordenschat.

    De import staat binnenin om dezelfde kringloop te vermijden als elders in dit bestand:
    navigation_lotc leunt op de menu-opbouw, en die komt via de routes hier langs.

    Leeg blijft leeg. Het filter staat inmiddels op elke plek waar een iconnaam uit
    GEGEVENS komt, en een deel daarvan is optioneel (``submit.icon`` is None zonder
    icoon, een menu-item zonder icoon geeft ''). Zonder deze regel zou zo'n plek een
    lege naam gaan vertalen en waarschuwen over niets.
    """
    from opi.web.navigation_lotc import to_nldd_icon

    if not naam:
        return naam

    return to_nldd_icon(naam)


# lotc-forms hoort achteraan; zie de moduledocstring.
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent / "templates_lotc"

# Losse Jinja2Templates, met een eigen loader. setup_components hangt de LOTC-
# componenttemplates aan loader.searchpath, dus dit moet een FileSystemLoader blijven.
templates_lotc = Jinja2Templates(directory=str(TEMPLATES_LOTC_DIR))
if not isinstance(templates_lotc.env.loader, FileSystemLoader):
    raise TypeError("templates_lotc env loader must be a FileSystemLoader for the LOTC search path")

# De catalogusmap erbij, achteraan. Een dienst draagt alles
# wat hij is in zijn eigen map (RC-36), dus ook zijn LOTC-sjablonen; zonder dit zoekpad
# levert "<dienst>/section-detail.html.j2" hier TemplateNotFound. Achteraan zodat een
# bestand in templates_lotc/ nooit overschaduwd kan worden door een gelijknamig
# dienstsjabloon.
templates_lotc.env.loader.searchpath.append(str(CATALOG_DIR))

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

# De globals en filters die de sjablonen aanroepen: version_info(), static_url() en de
# filters hieronder.
templates_lotc.env.globals["version"] = VERSION
templates_lotc.env.globals["build_date"] = BUILD_DATE
templates_lotc.env.globals["version_info"] = get_version_info
templates_lotc.env.globals["static_url"] = static_url
# De attribuutbundel van een formulierveld, voor LOTC's :attrs-spread. Vervangt de
# macro's die in de roos-templates attribuut-TEKST in de tag schreven; zie
# opi/forms/lotc_attrs.py voor waarom dat bij LOTC niet kan.
templates_lotc.env.globals["field_attrs"] = field_attrs


# Hier stonden twee overgangsdingen. render_roos() rendeerde een dienstblok zonder
# tegenhanger in de ROOS-omgeving en zette het resultaat hier als HTML neer; daarna kwam
# lotc_counterpart(), dat per dienstsjabloon zocht of er een ``-lotc``-versie naast lag en
# het blok anders OVERSLOEG. Allebei waren ze er zolang er twee bouwlijnen waren.
#
# Er is er nog een. Elke dienst heeft nog EEN sjabloon, onder zijn eigen naam, en de
# projectpagina rendert dat gewoon. Dat elke dienst dat sjabloon ook echt heeft, toetst
# tests/test_lotc_dienstblokken.py.


# De widgettemplates delen macro's die een attribuutwaarde escapen (optional_attr,
# bool_attr). De adapter rendert die macro's in DEZE omgeving, dus hier hoort het filter te
# staan. Zonder valt de formulierlaag om op elk veld dat de macro's gebruikt.

templates_lotc.env.filters["attr_escape"] = attr_escape

# De foutmelding bij een formulierveld zichtbaar maken. Onze kopie van
# templates_lotc/components/_forms.j2 roept dit filter aan op de besturing; zie
# bedraad_foutmelding voor de meting waarom dat nodig is en wanneer het weg kan.
templates_lotc.env.filters["foutbedrading"] = bedraad_foutmelding

# Onze eigen iconnamen (de woordenschat van het oude design system, die nog in de
# dienstdefinities staat) naar de NLDD-woordenschat. Als FILTER en niet vooraf in de data,
# omdat de dienstdefinities de bron zijn en die de eigen naam dragen.
# Zonder deze vertaling rendert een icoon leeg - stil, want een onbekende naam levert geen
# fout op. Zo misten de dienstkaarten in de wizard hun iconen, op PostgreSQL na: "database"
# heet toevallig in beide woordenschatten hetzelfde.
templates_lotc.env.filters["nldd_icon"] = _to_nldd_icon
templates_lotc.env.filters["service_name"] = get_service_name
templates_lotc.env.filters["service_definition"] = get_service_definition_for_entry
# Een aliaswaarde die naar platformvariabelen VERWIJST is geen geheim: hij noemt waar de
# waarde vandaan komt, en dat is juist wat je wilt zien. Een letterlijke waarde kan er
# wel een zijn. Dat oordeel staat bij de dienst zelf (AliasesService.owned_value_is_secret)
# en wordt hier als filter beschikbaar gemaakt, zodat het sjabloon niet zijn eigen regel
# verzint over wat een verwijzing is.
templates_lotc.env.filters["is_verwijzing"] = _alias_is_reference
templates_lotc.env.filters["dutch_date"] = format_dutch_date
templates_lotc.env.filters["rrule_schedule"] = format_rrule_schedule
templates_lotc.env.filters["deployment_action_key"] = deployment_action_key

# Als GLOBAL en niet als context: de hulproute rendert een .html.j2 met alleen het verzoek
# erin (router_wizard.service_help), dus een hulptekst die gegevens nodig heeft kan er
# anders niet bij. Een uitzondering voor dit ene sjabloon in die route zou de volgende
# hulptekst met gegevens weer een uitzondering kosten.
templates_lotc.env.globals["alias_variabelen"] = alias_variabelen

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
