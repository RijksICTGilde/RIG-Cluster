"""The allowed values of service config fields in the OpenAPI document.

The choices already exist, because the form in the portal fills its selects with them: the
field declares a ``values_provider`` and the provider yields the options. This module puts
that same declaration in the OpenAPI document, so no second list can drift from it.

Two kinds, and the difference is not cosmetic:

* a fixed list becomes ``x-choices``, an enumeration of ``{const, title, description}``.
  With an ``enum`` on the field as well (the config model knows the choice itself) those are
  the same values and ``x-choices`` only adds the label per value. Without an ``enum`` the
  list is what the portal OFFERS within a wider format, and then ``enum`` is deliberately
  absent: documenting narrower than the API accepts breaks a generated client on an existing
  project file;
* a list that comes from the project (its components, its attachments, the peers) becomes
  ``x-choices-source``, with the endpoint that yields the list and where in that answer the
  values stand. An enumeration here would be one arbitrary project's snapshot.

The annotation happens on the GENERATED document and not on the Pydantic models themselves,
which keeps the recorded schema fragments next to each service (``<service>.v1.0.json``) pure
model, and keeps a list that differs per cluster (sleep-mode offers an extra five minutes on
the sandbox) in the document that cluster serves instead of in a file in git.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from opi.forms.visualizers.providers import PROVIDER_REGISTRY, UNDECLARED_SOURCE, OptionsSource
from opi.services.catalog.base import CHOICES_KEY, CHOICES_PROVIDER_KEY, CHOICES_SOURCE_KEY, ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.services.catalog.base import Service

logger = logging.getLogger(__name__)

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
"""The text that accompanies ``info.description``.

The two keys above are extensions of our own, so the document itself explains them: whoever
opens ``/docs`` or ``/openapi.json`` has everything at hand. It says in so many words why a
choice list without an ``enum`` exists, because that is the one point a reader can get wrong:
reading ``x-choices`` as validation would trip a generated client over an existing project
file whose value the portal does not offer but the API does accept.
"""

#: The generated config routes, which carry the service and the layer in the path.
_CONFIG_ROUTE_RE = re.compile(
    r"^/api/v2/projects/\{project_name\}/services/(?P<service>[^/]+)/config/(?P<target>[a-z-]+)"
)

#: The key of a path segment, without an ``[*]`` index or a ``{service}`` filter.
_SEGMENT_KEY_RE = re.compile(r"^([^\[\{]+)")


def annotate_config_choices(openapi_schema: dict[str, Any]) -> None:
    """Put the allowed values on the config fields of *openapi_schema*, in place."""
    schemas = openapi_schema.get("components", {}).get("schemas", {})
    if not schemas:
        return

    # Collect first, write afterwards. Two layers of the same service can use the same config
    # model (cross-domain-access on project and deployment), and then one schema in the
    # document serves both. When two layers say something different about the same field there
    # is no answer that holds for both, so nothing is written: no choice list beats a choice
    # list that is untrue for half of its readers.
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

        for yaml_path, examples in _examples_for(service, layer).items():
            node = _locate(body, yaml_path, schemas)
            if node is not None:
                _attach_examples(node, examples, schemas)

    for node, proposed in proposals.values():
        first = proposed[0]
        if any(annotation != first for annotation in proposed):
            logger.debug("Keuzelijst overgeslagen: twee lagen delen dit schema en bieden verschillende waarden")
            continue
        node.update(first)

    _annotate_declared_providers(schemas)


def _annotate_declared_providers(schemas: dict[str, Any]) -> None:
    """Annotate every field that declares its own provider, wherever it stands.

    The loop above runs over the config routes; this one runs over the whole document, so a
    field outside those routes (a request model) gets the same choice list by declaring one
    line. The marker comes off afterwards: it is for us and not for the reader.
    """
    for schema in schemas.values():
        for node in (schema.get("properties") or {}).values():
            if not isinstance(node, dict):
                continue
            provider_name = node.pop(CHOICES_PROVIDER_KEY, None)
            if not provider_name:
                continue
            annotation = _annotation(str(provider_name), node)
            if annotation is not None:
                node.update(annotation)


def _examples_for(service: Service, layer: ConfigLayer) -> dict[str, list[str]]:
    """The example values per yaml path, out of the form section of *service*.

    Only the ``examples`` of the visualizer, not its ``placeholder``. A placeholder is
    written to stand in an empty input field and is as often a hint as a value
    ("Naam van de applicatie"); publishing that as an example would hold an impossible value
    out to a client. An example is meant to be sendable.
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
    """Put *examples* on *node*, or on its items when the field is a list.

    ``examples`` in JSON Schema are instances of the schema they stand on, so on a list field
    (``match``) an instance is a LIST, while the examples the form shows are single values.
    Those belong one layer deeper, on the items, where a client reads them as "this is what an
    element looks like" and not as "this is the whole list".
    """
    items_branch = _branch_with(node, "items", schemas)
    target = _resolve(items_branch["items"], schemas) if items_branch else node
    target.setdefault("examples", examples)


def _annotation(provider_name: str, property_schema: dict[str, Any]) -> dict[str, Any] | None:
    """The annotation for one field, or None when there is nothing to say."""
    provider_class = PROVIDER_REGISTRY.get(provider_name)
    if provider_class is None:
        return None

    source = getattr(provider_class, "options_source", UNDECLARED_SOURCE)
    if source is UNDECLARED_SOURCE:
        # Without a declaration it is not knowable whether an empty list means "no choices"
        # or "no project context", and a guess documents the wrong one.
        logger.warning("Provider %s zegt niet waar zijn opties vandaan komen; geen keuzelijst in de API", provider_name)
        return None
    if isinstance(source, OptionsSource):
        return {CHOICES_SOURCE_KEY: source.as_json()}

    if _is_boolean(property_schema):
        # Yes/No comes out of the form as the strings "true"/"false" while the API wants a
        # real JSON boolean. The type says it all here; the options would only suggest a
        # wrong shape.
        return None

    choices = [_choice(option) for option in provider_class().get_options() if option.get("value") not in (None, "")]
    return {CHOICES_KEY: choices} if choices else None


def _choice(option: dict[str, Any]) -> dict[str, Any]:
    """One option in JSON Schema idiom: the value as ``const``, the label as ``title``."""
    choice: dict[str, Any] = {"const": option["value"], "title": option.get("label", option["value"])}
    if option.get("description"):
        choice["description"] = option["description"]
    return choice


def _is_boolean(property_schema: dict[str, Any]) -> bool:
    branches = [property_schema, *property_schema.get("anyOf", [])]
    return any(branch.get("type") == "boolean" for branch in branches)


def _walk(editables: list[Editable]) -> list[Editable]:
    """Every editable, the children of a list field (a cross-domain rule) included."""
    found: list[Editable] = []
    for editable in editables:
        found.append(editable)
        if editable.children:
            found.extend(_walk(editable.children))
    return found


def _locate(body: dict[str, Any], yaml_path: str, schemas: dict[str, Any]) -> dict[str, Any] | None:
    """The piece of schema *yaml_path* comes out at, or None.

    The path of an editable stands in project-file terms
    (``services/sleep-mode/config/wake-mode``) while the body of the PUT is the config block
    itself, so everything before the ``config`` segment is the way to the body and is skipped.
    """
    segments = yaml_path.split("/")
    index = next((i for i, segment in enumerate(segments) if _key(segment) == "config"), None)
    if index is None:
        return None

    # ``config[*]`` means the config block is itself a list (the storage mounts).
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
    """Descend into the items of the list once per ``[*]`` in the segment.

    When the document holds no list this stays where it is. A service may show its list to the
    outside as one entry (``Service.api_singular_lists``), and the path of the editable then
    points at the element itself; carrying on to ``None`` would silently drop the choice list
    of every field inside such an entry.
    """
    for _ in range(segment.count("[*]")):
        if node is None:
            return None
        branch = _branch_with(node, "items", schemas)
        if branch is None:
            return _resolve(node, schemas)
        node = _resolve(branch["items"], schemas)
    return node


def _property(node: dict[str, Any], name: str, schemas: dict[str, Any]) -> dict[str, Any] | None:
    branch = _branch_with(node, "properties", schemas)
    if branch is None:
        return None
    return branch["properties"].get(name)


def _branch_with(node: dict[str, Any], key: str, schemas: dict[str, Any]) -> dict[str, Any] | None:
    """The branch of *node* that carries *key*.

    An optional field stands in the schema as ``anyOf: [{...}, {"type": "null"}]``, so the
    object or the list sits one layer deeper.
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
    """Follow a ``$ref``; every model stands in ``components/schemas``."""
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


def _service_for(name: str) -> Service | None:
    try:
        return SERVICES.get(ServiceType(name))
    except ValueError:
        return None


def _layer_for(target: str) -> ConfigLayer | None:
    try:
        return ConfigLayer(target)
    except ValueError:
        return None
