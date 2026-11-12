"""The template environment, on Lord of the Components.

The activation order of the design systems is fixed: ``lotc-forms`` has to come last, after
the visual theme, or the input fields do not resolve.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import markupsafe
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemLoader
from lord_of_the_components import get_static_roots, setup_components

from opi.core.config import BUILD_DATE, VERSION, settings
from opi.core.version import get_version_info
from opi.forms.lotc_attrs import attr_escape, field_attrs
from opi.http.auth.authz import template_can as _template_can
from opi.http.render.template_helpers import (
    CATALOG_DIR,
    cluster_tag,
    format_dutch_date,
    format_rrule_schedule,
    format_step_duration,
    get_service_definition_for_entry,
    get_service_name,
    shorten_image_digest,
    static_url,
)
from opi.services.catalog.aliases.overview import alias_variabelen
from opi.services.catalog.aliases.references import is_reference as _alias_is_reference
from opi.services.event_interpreter import group_component_failures
from opi.services.project_environment import environment_label
from opi.services.registry import deployment_action_key

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


def csp_nonce(request: Request) -> str:
    """The CSP nonce of this request, empty by default.

    A test request without state is fine; the Report-Only policy measures without a nonce.
    """
    return getattr(getattr(request, "state", None), "csp_nonce", "")


def _to_nldd_icon(name: str | None) -> str | None:
    """Translate an icon name out of the service definitions into the NLDD vocabulary.

    The import sits inside to avoid the same cycle as elsewhere in this file:
    navigation_lotc leans on the menu construction, and that passes here through the routes.

    Empty stays empty. The filter now sits at every place where an icon name comes out of
    DATA, and part of that is optional (``submit.icon`` is None without an icon, a menu item
    without an icon gives ''). Without this line such a place would translate an empty name
    and warn about nothing.
    """
    from opi.http.render.navigation_lotc import to_nldd_icon

    if not name:
        return name

    return to_nldd_icon(name)


# lotc-forms belongs last; see the module docstring.
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent.parent / "templates_lotc"

# A separate Jinja2Templates, with a loader of its own. setup_components hangs the LOTC
# component templates on loader.searchpath, so this has to stay a FileSystemLoader.
templates_lotc = Jinja2Templates(directory=str(TEMPLATES_LOTC_DIR))
if not isinstance(templates_lotc.env.loader, FileSystemLoader):
    raise TypeError("templates_lotc env loader must be a FileSystemLoader for the LOTC search path")

# The catalogue directory added, last. A service carries everything it is in its own
# directory (RC-36), its LOTC templates included; without this search path
# "<service>/section-detail.html.j2" gives TemplateNotFound here. Last, so a file in
# templates_lotc/ can never be shadowed by a service template of the same name.
templates_lotc.env.loader.searchpath.append(str(CATALOG_DIR))

# autoescape is required: the renderers escape prop values and treat content as
# already-safe Markup. With autoescape off, user data would not be escaped.
templates_lotc.env.autoescape = True
setup_components(templates_lotc.env, design_systems=DESIGN_SYSTEMS, htmx=True)

# An own component next to the LOTC ones goes in with merge_fragment on the registry plus
# the own template directory on the searchpath; see features/lotc-bouwlijn.md.

# The globals and filters the templates call: version_info(), static_url() and the filters
# below.
templates_lotc.env.globals["version"] = VERSION
templates_lotc.env.globals["build_date"] = BUILD_DATE
templates_lotc.env.globals["cluster_tag"] = cluster_tag(settings.CLUSTER_MANAGER)
templates_lotc.env.globals["version_info"] = get_version_info
templates_lotc.env.globals["static_url"] = static_url
# The grouping of component failures. As a global and not as a context key, so every render
# route of the progress fragment gets it: deriving it in one of the two context builders
# means the other route quietly shows the ungrouped picture.
templates_lotc.env.globals["group_component_failures"] = group_component_failures
# The attribute bundle of a form field, for LOTC's :attrs spread. See opi/forms/lotc_attrs.py
# for why writing attribute TEXT into the tag cannot work with LOTC.
templates_lotc.env.globals["field_attrs"] = field_attrs

# The CSP nonce of the current request, for nonce="{{ csp_nonce }}" on every own <script>.
# As a context_processor and not as a global: the value differs per request.
templates_lotc.context_processors.append(lambda request: {"csp_nonce": csp_nonce(request)})

# can(action, project=..., deployment=...) for the templates: the same rules as the gate
# behind the button. A global with the context along, so a separately rendered fragment has
# it too; it reads the request out of the context.
templates_lotc.env.globals["can"] = _template_can


# Every service has ONE template, under its own name, and the project page renders that.
# That every service really has it is checked by tests/test_lotc_dienstblokken.py.


# The widget templates share macros that escape an attribute value (optional_attr,
# bool_attr). The adapter renders those macros in THIS environment, so the filter belongs
# here. Without it the form layer falls over on every field that uses the macros.

templates_lotc.env.filters["attr_escape"] = attr_escape

# Our own icon names (the vocabulary of the old design system, which is still in the service
# definitions) into the NLDD vocabulary. As a FILTER and not up front in the data, because
# the service definitions are the source and those carry the own name.
# Without this translation an icon renders empty, and silently, because an unknown name gives
# no error.
templates_lotc.env.filters["nldd_icon"] = _to_nldd_icon
templates_lotc.env.filters["service_name"] = get_service_name
templates_lotc.env.filters["service_definition"] = get_service_definition_for_entry
# An alias value that REFERS to platform variables is not a secret: it names where the value
# comes from, and that is exactly what you want to see. A literal value can be one. That
# judgement sits with the service itself (AliasesService.owned_value_is_secret) and is made
# available here as a filter, so the template does not invent its own rule about what a
# reference is.
templates_lotc.env.filters["is_verwijzing"] = _alias_is_reference
templates_lotc.env.filters["dutch_date"] = format_dutch_date
templates_lotc.env.filters["stapduur"] = format_step_duration
templates_lotc.env.filters["rrule_schedule"] = format_rrule_schedule
templates_lotc.env.filters["deployment_action_key"] = deployment_action_key
# The digest in an image reference shortened to twelve characters. As a filter and not as a
# ``[:19]`` in the template: the deployment card shows two image references (the running one
# and the configured one), so otherwise the bound would sit in two places.
templates_lotc.env.filters["korte_digest"] = shorten_image_digest

# As a GLOBAL and not as context: the help route renders a .html.j2 with only the request in
# it (router_wizard.service_help), so a help text that needs data cannot reach it otherwise.
templates_lotc.env.globals["alias_variabelen"] = alias_variabelen
# The OTAP marking as a label: per deployment its own, at project level the highest of its
# deployments. As a GLOBAL and not per page in the context, so the default and the derivation
# sit in ONE place (opi/services/project_environment.py).
templates_lotc.env.globals["environment_label"] = environment_label

templates_lotc.env.add_extension("jinja2.ext.i18n")

# The filesystem roots that fill the /static/lotc/ URL space: the core styles plus the bundle
# of every activated design system. Those live in different installed packages, so the route
# below tries them in order.
LOTC_STATIC_ROOTS = [Path(root).resolve() for root in get_static_roots()]


def resolve_lotc_static(rel: str) -> Path | None:
    """Look ``rel`` up under the LOTC static roots; None when it is not there.

    A request for ``/static/lotc/<rest>`` lands on ``<root>/lotc/<rest>``, first hit wins.
    The ``is_relative_to`` check keeps path traversal out: ``rel`` comes out of the URL and
    may not point outside its root.
    """
    for root in LOTC_STATIC_ROOTS:
        candidate = (root / "lotc" / rel).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    return None


def process_components_lotc(html: object) -> markupsafe.Markup:
    """Render component tags in HTML that only comes into being during the request.

    The preprocessor of the extension only runs when a template is compiled, so a string
    assembled during the request (form HTML out of the editables, for instance) still has to
    pass it.

    The value is turned into text first. That is not for form's sake: a template applying
    this filter to an unset variable gives an Undefined, and handing that straight to
    from_string gives a TypeError out of the Jinja compiler instead of the empty output that
    is the habit everywhere else.
    """
    rendered = templates_lotc.env.from_string(str(html)).render()
    return markupsafe.Markup(rendered)  # noqa: S704  # the source is a constant from the component layer, not input


templates_lotc.env.filters["process_components"] = process_components_lotc
