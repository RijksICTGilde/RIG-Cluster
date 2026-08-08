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

import markupsafe
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemLoader
from lord_of_the_components import get_static_roots, setup_components
from lord_of_the_components.registry import ComponentRegistry

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

logger = logging.getLogger(__name__)

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

# Componenten die ZAD zelf levert omdat LOTC ze nog niet heeft. Zie
# opi/templates_lotc_zad/registry.json voor het waarom per component.
#
# Ze staan op een EIGEN owner-naam en niet op die van een design system: ze zijn niet
# van NLDD, en dat hoort in de registry te zien zijn. Dit is de weg die het LOTC-project
# hiervoor aanwijst.
#
# Tijdelijk, en met opzet zo dat het opruimen een verwijdering is: de aanroep in de
# templates is die van LOTC, dus zodra hun versie er is vervalt alleen dit blok.
_ZAD_COMPONENTS_DIR = Path(__file__).parent.parent / "templates_lotc_zad"
_ZAD_OWNER_THEME = "zad"

_lotc_extension = next(
    ext
    for ext in templates_lotc.env.extensions.values()
    if isinstance(getattr(ext, "registry", None), ComponentRegistry)
)
_lotc_extension.registry.merge_fragment(_ZAD_COMPONENTS_DIR / "registry.json", theme=_ZAD_OWNER_THEME)
templates_lotc.env.loader.searchpath.append(str(_ZAD_COMPONENTS_DIR))

# Dezelfde globals en filters als de roos-omgeving, zodat een omgezet template niet
# ook nog zijn aanroepen naar version_info(), static_url() en de filters hoeft te
# veranderen. De omzetting gaat over componenten, niet over de rest van het template.
templates_lotc.env.globals["version"] = VERSION
templates_lotc.env.globals["build_date"] = BUILD_DATE
templates_lotc.env.globals["version_info"] = get_version_info
templates_lotc.env.globals["static_url"] = static_url

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


def process_components_lotc(html: str) -> markupsafe.Markup:
    """Render componenttags in HTML die pas tijdens de aanvraag ontstaat.

    De voorbewerker van de extensie draait alleen bij het compileren van een template,
    dus een string die tijdens de aanvraag wordt samengesteld (bijvoorbeeld formulier-
    HTML uit de editables) moet er alsnog langs.
    """
    rendered = templates_lotc.env.from_string(html).render()
    return markupsafe.Markup(rendered)  # noqa: S704


templates_lotc.env.filters["process_components"] = process_components_lotc


def _precompile_all_templates() -> int:
    """Compileer elk LOTC-template bij het starten; geef terug hoeveel er lukten.

    Dit is geen opwarming maar een noodzaak, en het is de tegenhanger van een bug in
    LOTC 26ab110. De voorbewerker zet elke ``<c-*>``-tag om in een aanroep van een
    renderer die pas bestaat zodra een template dat het component gebruikt ZELF als
    hoofdtemplate is gecompileerd. Een ``{% include %}`` telt daar niet voor mee.

    Gevolg zonder deze stap: een pagina waarvan de componenten alleen in ingevoegde
    deeltemplates staan, faalt bij het renderen met "'_lotc_jinja_card' is undefined" -
    maar alleen als geen andere pagina dat component eerder toevallig registreerde. Dat
    maakt het een fout die van de volgorde van bezoeken afhangt, en die in een test
    anders uitpakt dan in de browser.

    Door hier alles een keer te compileren staan alle renderers klaar voordat het
    eerste verzoek binnenkomt. Templates die niet compileren worden overgeslagen; welke
    dat zijn en waarom staat in tests/test_lotc_conversion.py.
    """
    compiled = 0
    for path in sorted(TEMPLATES_LOTC_DIR.rglob("*.j2")):
        name = str(path.relative_to(TEMPLATES_LOTC_DIR))
        try:
            templates_lotc.env.get_template(name)
        except Exception as error:
            logger.debug("LOTC-template %s compileert nog niet: %s", name, error)
        else:
            compiled += 1
    return compiled


PRECOMPILED_TEMPLATE_COUNT = _precompile_all_templates()
