"""Toegestane waarden van service-configvelden in de OpenAPI-uitvoer.

Een client die een service configureert (de zad-cli, een agent) leest het schema van de
PUT-body en weet daarmee welke velden er zijn, maar niet welke waarden zo'n veld accepteert.
Voor sleep-mode is dat het verschil tussen "sleep-after-deploy is een string" en "je kunt
hier 4h, 8h, ... 168h kiezen": zonder dat laatste blijft raden of uitproberen over.

Die keuzes bestaan al, want het formulier in de portal vult zijn selects ermee: het veld
declareert een ``values_provider`` en de provider levert de opties. Dit bestand zet diezelfde
declaratie in het OpenAPI-document, zodat er geen tweede lijst ontstaat die binnen een
release uit de pas loopt.

Twee soorten, en het verschil is niet cosmetisch:

* een vaste lijst -> ``x-choices``, een opsomming van ``{const, title, description}``.
  Staat er ook een ``enum`` op het veld (het configmodel kent de keuze dan zelf), dan zijn
  dat dezelfde waarden en voegt ``x-choices`` alleen het label per waarde toe. Staat er geen
  ``enum``, dan is de lijst wat de portal AANBIEDT binnen een breder formaat -- en dan is
  ``enum`` er bewust niet, want narrower documenteren dan de API accepteert breekt een
  gegenereerde client op een bestaand projectbestand;
* een lijst die uit het project komt (de componenten, de bijlagen, de peers) ->
  ``x-choices-source``, met het endpoint dat de lijst levert en waar in dat antwoord de
  waarden staan. Een opsomming zou hier een momentopname van een willekeurig project zijn.

De annotatie gebeurt op het GEGENEREERDE document en niet op de Pydantic-modellen zelf.
Twee redenen: de vastgelegde schemafragmenten naast elke service (``<service>.v1.0.json``)
blijven daarmee puur het model, en een lijst die per cluster verschilt (sleep-mode biedt in
de sandbox een extra van vijf minuten) hoort thuis in het document dat dat cluster serveert,
niet in een bestand in git.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from opi.forms.visualizers.providers import PROVIDER_REGISTRY, UNDECLARED_SOURCE, OptionsSource
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable

logger = logging.getLogger(__name__)

CHOICES_KEY = "x-choices"
"""De waarden die dit veld aanbiedt, als ``{const, title, description}``."""

CHOICES_SOURCE_KEY = "x-choices-source"
"""Waar de waarden vandaan komen als ze per project verschillen."""

API_DESCRIPTION = """\
GitOps Operations and Project Infrastructure API for self-service Kubernetes environments.

## Allowed values on service config fields

Service config schemas carry the same choices the portal offers in its forms, so a client
does not have to guess or probe. Two keys, and the difference matters:

* `enum` (standard JSON Schema) is present when the field itself is closed: those values,
  and nothing else, are accepted.
* `x-choices` lists what the portal OFFERS: an array of `{const, title, description?}`.
  Alongside an `enum` it only adds a label per value. Without one the field accepts more
  than the list shows (`sleep-after-deploy` takes any duration, `90m` included), so treat
  it as a menu, not as validation.
* `x-choices-source` appears when the values come from the project itself (its components,
  its attachments, the deployments of a peer). It holds `{description, endpoint?, path?}`:
  call that endpoint for the current list, and read the values at `path`. An enumeration
  here would be one project's snapshot and wrong for every other.
"""
"""De begeleidende tekst bij ``info.description``.

De twee sleutels hierboven zijn eigen uitbreidingen, en een uitbreiding die nergens wordt
uitgelegd is voor een lezer niet te onderscheiden van ruis. De uitleg hoort daarom in het
document zelf en niet alleen in een bestand in git: wie ``/docs`` of ``/openapi.json``
openslaat heeft alles bij de hand.

Waarom een keuzelijst zonder ``enum`` bestaat, staat er met zoveel woorden bij. Dat is het
enige punt waarop een lezer zich kan vergissen: ``x-choices`` lezen als validatie zou een
gegenereerde client laten struikelen over een bestaand projectbestand met een waarde die de
portal niet aanbiedt maar de API wel accepteert.
"""

#: De gegenereerde configroutes, waar service en laag in het pad staan.
_CONFIG_ROUTE_RE = re.compile(
    r"^/api/v2/projects/\{project_name\}/services/(?P<service>[^/]+)/config/(?P<target>[a-z-]+)"
)

#: De sleutel van een pad-segment, zonder ``[*]``-index of ``{service}``-filter.
_SEGMENT_KEY_RE = re.compile(r"^([^\[\{]+)")


def annotate_config_choices(openapi_schema: dict[str, Any]) -> None:
    """Zet de toegestane waarden bij de configvelden in *openapi_schema* (in place)."""
    schemas = openapi_schema.get("components", {}).get("schemas", {})
    if not schemas:
        return

    # Eerst verzamelen, dan pas schrijven. Twee lagen van dezelfde service kunnen hetzelfde
    # configmodel gebruiken (cross-domain-access op project en deployment), en dan is er één
    # schema in het document voor allebei. Zeggen twee lagen iets anders over hetzelfde veld,
    # dan is er geen antwoord dat voor allebei klopt en schrijven we niets: liever geen
    # keuzelijst dan een keuzelijst die voor de helft van de lezers onwaar is.
    proposals: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    for path, item in openapi_schema.get("paths", {}).items():
        match = _CONFIG_ROUTE_RE.match(path)
        if not match or "put" not in item:
            continue
        body = _request_body_schema(item["put"])
        if body is None:
            continue
        service = _service_for(match.group("service"))
        layer = _layer_for(match.group("target"))
        if service is None or layer is None:
            continue
        for editable in _walk(service.config_editables(layer)):
            if not editable.values_provider:
                continue
            node = _locate(body, editable.yaml_path, schemas)
            if node is None:
                continue
            annotation = _annotation(editable.values_provider, node)
            if annotation is None:
                continue
            proposals.setdefault(id(node), (node, []))[1].append(annotation)

        for yaml_path, voorbeelden in _examples_for(service, layer).items():
            node = _locate(body, yaml_path, schemas)
            if node is not None:
                _attach_examples(node, voorbeelden, schemas)

    for node, proposed in proposals.values():
        first = proposed[0]
        if any(annotation != first for annotation in proposed):
            logger.debug("Keuzelijst overgeslagen: twee lagen delen dit schema en bieden verschillende waarden")
            continue
        node.update(first)


# --- voorbeelden ------------------------------------------------------------


def _examples_for(service: Any, layer: ConfigLayer) -> dict[str, list[str]]:
    """De voorbeeldwaarden per yaml-pad, uit de formuliersectie van *service*.

    Een veld met een vrij formaat is voor een API-gebruiker het lastigst: bij ``match`` van
    sleep-mode weet je dat er een string in moet, maar niet dat het om een glob op de
    deploymentnaam gaat. Het formulier heeft dat antwoord wel, want daar staan voorbeelden
    onder het veld. Die horen dus ook in het document.

    Alleen ``examples`` van de visualizer, niet zijn ``placeholder``. Een placeholder is
    geschreven om in een leeg invoerveld te staan en is even vaak een aanwijzing als een
    waarde ("Naam van de applicatie"); dat als voorbeeld publiceren zou een client een
    onmogelijke waarde voorhouden. Een voorbeeld is bedoeld om te kunnen versturen.
    """
    section = service.config_form_section(layer)
    if section is None:
        return {}

    found: dict[str, list[str]] = {}

    def walk(visualizers: list[Any]) -> None:
        for visualizer in visualizers or []:
            editable = getattr(visualizer, "editable", None)
            examples = getattr(visualizer, "examples", None)
            if editable is not None and examples:
                found[editable.yaml_path] = list(examples)
            walk(getattr(visualizer, "children", None) or [])

    walk(section.editables)
    return found


def _attach_examples(node: dict[str, Any], examples: list[str], schemas: dict[str, Any]) -> None:
    """Zet *examples* op *node*, of op de items als het veld een lijst is.

    ``examples`` in JSON Schema zijn instanties van het schema waar ze op staan. Bij een
    lijstveld (``match``) is een instantie dus een LIJST, terwijl de voorbeelden die het
    formulier toont losse waarden zijn; die horen daarom een laag dieper, op de items. Zo
    leest een client ze als "zo ziet een element eruit" en niet als "dit is de hele lijst".
    """
    items_branch = _branch_with(node, "items", schemas)
    doel = _resolve(items_branch["items"], schemas) if items_branch else node
    doel.setdefault("examples", examples)


# --- wat een veld aanbiedt --------------------------------------------------


def _annotation(provider_name: str, property_schema: dict[str, Any]) -> dict[str, Any] | None:
    """De annotatie voor één veld, of None als er niets te zeggen valt."""
    provider_class = PROVIDER_REGISTRY.get(provider_name)
    if provider_class is None:
        return None

    source = getattr(provider_class, "options_source", UNDECLARED_SOURCE)
    if source is UNDECLARED_SOURCE:
        # Zonder declaratie weten we niet of een lege lijst "geen keuzes" of "geen
        # projectcontext" betekent, en gokken levert een verkeerde documentatie op.
        logger.warning("Provider %s zegt niet waar zijn opties vandaan komen; geen keuzelijst in de API", provider_name)
        return None
    if isinstance(source, OptionsSource):
        return {CHOICES_SOURCE_KEY: source.as_json()}

    if _is_boolean(property_schema):
        # Ja/Nee komt uit het formulier als de strings "true"/"false", terwijl de API een
        # echte JSON-boolean wil. Het type zegt hier al alles; de opties zouden alleen maar
        # een verkeerde vorm suggereren.
        return None

    choices = [_choice(option) for option in provider_class().get_options() if option.get("value") not in (None, "")]
    return {CHOICES_KEY: choices} if choices else None


def _choice(option: dict[str, Any]) -> dict[str, Any]:
    """Eén optie als JSON-Schema-idioom: de waarde als ``const``, het label als ``title``."""
    choice: dict[str, Any] = {"const": option["value"], "title": option.get("label", option["value"])}
    if option.get("description"):
        choice["description"] = option["description"]
    return choice


def _is_boolean(property_schema: dict[str, Any]) -> bool:
    branches = [property_schema, *property_schema.get("anyOf", [])]
    return any(branch.get("type") == "boolean" for branch in branches)


# --- van yaml-pad naar plek in het schema -----------------------------------


def _walk(editables: list[Editable]) -> list[Editable]:
    """Alle editables, ook de kinderen van een lijstveld (een cross-domain-regel)."""
    found: list[Editable] = []
    for editable in editables:
        found.append(editable)
        if editable.children:
            found.extend(_walk(editable.children))
    return found


def _locate(body: dict[str, Any], yaml_path: str, schemas: dict[str, Any]) -> dict[str, Any] | None:
    """Het stukje schema waar *yaml_path* op uitkomt, of None.

    Het pad van een editable staat in projectbestand-termen
    (``services/sleep-mode/config/wake-mode``); de body van de PUT is het configblok zelf.
    Alles vóór het ``config``-segment hoort dus bij de weg naar de body en wordt overgeslagen.
    """
    segments = yaml_path.split("/")
    index = next((i for i, segment in enumerate(segments) if _key(segment) == "config"), None)
    if index is None:
        return None

    # ``config[*]`` betekent dat het configblok zelf een lijst is (de storage-mounts).
    node: dict[str, Any] | None = _descend_lists(body, segments[index], schemas)
    for segment in segments[index + 1 :]:
        if node is None:
            return None
        node = _property(node, _key(segment), schemas)
        if node is None:
            return None
        node = _descend_lists(node, segment, schemas)
    return node


def _key(segment: str) -> str:
    match = _SEGMENT_KEY_RE.match(segment)
    return match.group(1) if match else segment


def _descend_lists(node: dict[str, Any] | None, segment: str, schemas: dict[str, Any]) -> dict[str, Any] | None:
    """Per ``[*]`` in het segment één keer in de items van de lijst zakken."""
    for _ in range(segment.count("[*]")):
        if node is None:
            return None
        branch = _branch_with(node, "items", schemas)
        node = _resolve(branch["items"], schemas) if branch else None
    return node


def _property(node: dict[str, Any], name: str, schemas: dict[str, Any]) -> dict[str, Any] | None:
    branch = _branch_with(node, "properties", schemas)
    if branch is None:
        return None
    return branch["properties"].get(name)


def _branch_with(node: dict[str, Any], key: str, schemas: dict[str, Any]) -> dict[str, Any] | None:
    """De tak van *node* die *key* draagt.

    Een optioneel veld staat in het schema als ``anyOf: [{...}, {"type": "null"}]``, dus het
    object of de lijst zit dan één laag dieper.
    """
    node = _resolve(node, schemas)
    if key in node:
        return node
    for branch in node.get("anyOf", []):
        resolved = _resolve(branch, schemas)
        if key in resolved:
            return resolved
    return None


def _resolve(node: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    """Volg een ``$ref``; elk model staat in ``components/schemas``."""
    seen: set[str] = set()
    while isinstance(node, dict) and "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return {}
        seen.add(name)
        node = schemas.get(name, {})
    return node


def _request_body_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    content = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
    schema = content.get("schema")
    return schema if isinstance(schema, dict) else None


def _service_for(name: str) -> Any | None:
    try:
        return SERVICES.get(ServiceType(name))
    except ValueError:
        return None


def _layer_for(target: str) -> ConfigLayer | None:
    try:
        return ConfigLayer(target)
    except ValueError:
        return None
