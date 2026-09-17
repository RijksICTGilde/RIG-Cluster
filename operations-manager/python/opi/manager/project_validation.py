"""Structural validation of a complete project dict.

Extracted from ProjectManager so the ProjectStore can run the SAME checks on the
final state of a mutation, before anything is written or committed. Keeping one
implementation means there is no "less validated" write path: ProjectManager and
ProjectStore both call into here.

Runs AFTER json-schema validation (opi.core.project_schema.validate_project_schema)
and BEFORE any write or commit. Fails closed on the first violation.
"""

import logging
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from opi.core.cluster_config import CLUSTER_CONFIG
from opi.core.config import settings
from opi.core.project_schema import ProjectIntegrityError, age_pattern_violations
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning
from opi.handlers.project_file_handler import validate_attachment_couplings, validate_attachment_references
from opi.services import ServiceAdapter
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.config_settings import SettingError, check_setting_changes, check_settings
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.catalog.shared.storage import STORED_CONTEXT_KEY
from opi.services.postgres_scope import get_postgres_schemas
from opi.services.project import Project
from opi.services.registry import SERVICES, get_service, project_validating_services, property_owning_services
from opi.services.services import (
    service_entry_config,
    service_entry_data,
    service_entry_name,
    service_entry_schema_version,
)
from opi.services.services_enums import ServiceType
from opi.utils.naming import (
    RESERVED_DEPLOYMENT_NAMES,
    generate_extra_database_schema,
    normalize_registry_repo,
    registry_tag_owner,
    split_image_reference,
)
from opi.utils.project_utils import ComponentValidationError, validate_root_component

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opi.forms.editables.editable import Editable

logger = logging.getLogger(__name__)

#: This module validates a project file that ALREADY EXISTS -- on every save, and on
#: every reprocess and replay of a file nobody touched. A rule about how large a new
#: volume may be does not belong here: applying it to stored data would turn an older
#: project with a larger mount into a file that can no longer be saved at all, and a
#: PVC cannot shrink, so its owner could not comply either. Ceilings are enforced where
#: the value ARRIVES (the config API's request bodies and the form field); here the
#: shape is checked and the value is taken as it stands.
STORED_PROJECT_CONTEXT: dict[str, Any] = {STORED_CONTEXT_KEY: True}


def _accepted_config_fields(provider: Service, layer: ConfigLayer) -> list[str]:
    """The config field names a service accepts at ``layer``, for error guidance.

    Sources the service's own declarative field metadata: ``config_api_fields``
    (the API/YAML-accepted keys, derived from ``config_model_field_names`` for
    modelled services), falling back to the leaf names of ``config_editables`` for
    services whose config is a sequence with no flat field set (storage). Returns []
    when the service declares neither.
    """
    fields = provider.config_api_fields(layer)
    if fields:
        return fields
    # Sequence configs (storage) declare no flat field set, so read the leaf names off
    # the config_editables: the per-entry child fields (name/size/mount-path) when the
    # editable is a sequence, else the editable's own leaf.
    names: list[str] = []
    for editable in provider.config_editables(layer):
        leaves = editable.children or [editable]
        names.extend(child.yaml_path.rsplit("/", 1)[-1] for child in leaves)
    return names


def validation_reasons(error: ValidationError) -> str:
    """De redenen van een ValidationError, zoals ze aan een gebruiker getoond mogen worden.

    ``str(e)`` van pydantic is uitvoer voor een ontwikkelaar: hij zet er
    ``[type=value_error, input_value=..., input_type=dict]`` achter en een link naar
    errors.pydantic.dev. Dat kwam zo op het scherm van iemand die een webadres wilde
    wijzigen, met de afgekeurde waarde erin -- en die waarde kan een geheim zijn.

    ``error["msg"]`` draagt alleen de reden. Het voorvoegsel ``Value error, `` dat pydantic
    voor een ``model_validator`` zet valt eraf: de zin eromheen zegt al dat er iets ongeldig
    is, en "Value error" voegt daar niets aan toe wat de lezer verder helpt.
    """
    reasons = [error_entry["msg"].removeprefix("Value error, ") for error_entry in error.errors()]
    return "; ".join(reasons) or "waarde voldoet niet aan het model"


def _validate_one_config(block: ServiceConfigBlock, project_name: str) -> None:
    """Validate one service config block against its provider's typed model.

    Shared by the project-level and component-level walks. Skips services that are
    unknown or take no typed config. The block's ``from_version`` is the entry's stamped
    ``schema-version``, threaded through so the provider migrates an older config
    block forward before validating (None = current version). Fails closed: raises
    ProjectIntegrityError, with the service's own accepted-field list
    (config_api_fields / config_editables) appended so the message tells the user
    which keys the service accepts.
    """
    try:
        service_type = ServiceType(block.name)
    except ValueError:
        return  # unknown service name -- other validation handles it
    provider = get_service(service_type)
    model = provider.config_model_for(block.layer)
    if model is not None:
        try:
            if model is provider.config_model:
                provider.validate_config(block.config, from_version=block.from_version, context=STORED_PROJECT_CONTEXT)
            else:
                # A layer-specific model (per-mount clone state). OPI writes it, so there is no
                # stamped version to migrate from; validate the shape directly.
                model.model_validate(block.config)
        except ValidationError as e:
            accepted = _accepted_config_fields(provider, block.layer)
            hint = f" Geaccepteerde velden: {', '.join(accepted)}." if accepted else ""
            raise ProjectIntegrityError(
                f"Project '{project_name}': configuratie van service '{block.name}' {block.where} is ongeldig: "
                f"{validation_reasons(e)}.{hint}"
            ) from e

    _check_declared_settings(provider, block, project_name)


def _check_declared_settings(provider: Service, block: ServiceConfigBlock, project_name: str) -> None:
    """The service's declared latitude (RC-168) for one config block.

    A value the service opened up has to stay inside the bounds the SERVICE set, and may
    only sit on a layer the service opened it up on. Here rather than in the model, so the
    bound is stated once and the wizard, the API and a hand-edited file are judged by the
    same declaration. A service that declares nothing does no extra work here.

    Called for every block in the walk, including the ones whose config is a component
    PROPERTY (``user-env-vars``, ``aliases``): a setting on such a service is judged on
    its bounds and on its change, not on one of the two. What the refusal may repeat is
    ``_setting_refusal``'s call.

    Raises:
        ProjectIntegrityError: with the sentence the user reads.
    """
    try:
        check_settings(provider.config_settings(), block.config, block.layer)
    except SettingError as e:
        raise ProjectIntegrityError(
            f"Project '{project_name}': configuratie van service '{provider.service_type.value}' {block.where} "
            f"is ongeldig: {_setting_refusal(e, block)}"
        ) from e


def _setting_refusal(e: SettingError, block: ServiceConfigBlock) -> str:
    """The reason for a refused setting, as the value check and the change check both give it.

    On an owned-property block (``user-env-vars``, ``aliases``) the value is not repeated,
    for the reason ``_validate_owned_property`` next door already acts on: the property
    holds the component's own environment, and ``UserEnvVarsConfig`` accepts a plain
    ``dict[str, str]``, so a value read at a declared path there can be a pasted secret,
    and this message is both logged at WARNING and returned to the caller. For a change
    that holds for the PREVIOUS value too, which no check judges on its form again. The
    refusal is then rendered from the DECLARATION (the field and its room). For a block in
    a ``services:`` list the value is named, as it is the bound itself being quoted back.
    """
    # ``setting`` is set only on a refusal that names a value; a wrong-layer refusal
    # names none and reads the same either way.
    if block.owned_property is not None and e.setting is not None:
        return f"'{e.setting.path}' valt buiten zijn speelruimte. {e.setting.latitude()}"
    return str(e)


def _validate_one_data_block(name: str, raw: Any, layer: ConfigLayer, where: str, project_name: str) -> None:
    """Validate one service's DEFINE-side ``data`` block against its provider's model.

    The counterpart of ``_validate_one_config`` for the definitions a service stores
    rather than the configuration of a use. Skips services that define nothing at the
    layer. Fails closed, and reports only the validators' own reasons -- a definition
    holds the thing itself (an attachment's content), so pydantic's ``input_value``
    would put an encrypted blob, or worse a plaintext one, in the log and the response.
    """
    try:
        service_type = ServiceType(name)
    except ValueError:
        return  # unknown service name -- other validation handles it
    model = get_service(service_type).data_model_for(layer)
    if model is None:
        return  # service defines nothing at this layer
    try:
        model.model_validate(raw)
    except ValidationError as e:
        reasons = validation_reasons(e)
        raise ProjectIntegrityError(
            f"Project '{project_name}': gegevens van service '{name}' {where} zijn ongeldig: {reasons}."
        ) from None


@dataclass(frozen=True)
class ServiceConfigBlock:
    """One place in a project file where a service's configuration lives.

    ``location`` identifies the block ACROSS two versions of the same file -- the
    project itself, a named component, a named deployment, a named component inside a
    named deployment, and inside that a named mount where the service keeps a record per
    mount -- so a rule about a CHANGE can line the same block up in the old file and the
    new one. Without that key a reduction would be compared against an unrelated
    component's, or an unrelated mount's, value. ``where`` is that same place phrased for
    a user, and ``from_version`` is the entry's stamped schema version; both are for the
    value check.

    ``config`` is None for a service that is referenced without configuration, which is
    not the same as a service that is not referenced at all. That difference is the
    point: a service still selected with its config block deleted has fallen back to the
    service defaults, and that is a change worth judging.
    """

    location: str
    where: str
    name: str
    layer: ConfigLayer
    config: Any
    from_version: str | None
    #: Where the block sits by POSITION (``components/0/services/1/config``), for a
    #: message that points into the file. ``location`` cannot give that: it keys on names
    #: so it survives a reorder, and drops the indexes a path needs.
    path: str
    #: Set for the SYSTEM services whose config is a plain component property
    #: (``user-env-vars``, ``aliases``) instead of an entry in a ``services:`` list.
    owned_property: str | None = None


def iter_service_config_blocks(project_data: dict[str, Any]) -> Iterator[ServiceConfigBlock]:
    """Every place a service's configuration lives in a project file.

    One walk with two readers: the value check (``validate_service_configs``) uses
    ``where`` and ``from_version``, the change check
    (``validate_service_setting_changes``) uses ``location``. It used to be two walks
    over the same four layers, and they had already drifted apart -- the owned-property
    blocks were in only one of them, so a setting declared on such a service would have
    been judged on its bounds and never on its change.

    Covers every layer a config can live at: project-level service definitions
    (keycloak, namespace-postgres, auth-wall), component-level references
    (persistent-storage / temp-storage mounts, metrics-scraper port/path),
    deployment-level entries (clone state today), both shapes on the
    deployment-component layer, and the component properties that SYSTEM services own.
    """
    view = Project(project_data)
    for name in ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", [])):
        yield ServiceConfigBlock(
            location="project",
            where="op projectniveau",
            name=name,
            layer=ConfigLayer.PROJECT,
            config=view.service_config(name),
            from_version=service_entry_schema_version(view.service_entry(name)),
            path=f"services/{name}/config",
        )

    # Component-level service references (storage mounts, metrics port/path). Their
    # config lives on the component's service entry, not at project level, so the
    # project-level walk above never sees it.
    for comp_index, component in enumerate(project_data.get("components", []) or []):
        if not isinstance(component, dict):
            continue
        comp_name = component.get("name", "(onbekend)")
        location = f"component:{component.get('name')}"
        for entry_index, entry in enumerate(component.get("services", []) or []):
            name = service_entry_name(entry)
            if name is None:
                continue
            yield ServiceConfigBlock(
                location=location,
                where=f"in component '{comp_name}'",
                name=name,
                layer=ConfigLayer.COMPONENT,
                config=service_entry_config(entry),
                from_version=service_entry_schema_version(entry),
                path=f"components/{comp_index}/services/{entry_index}/config",
            )
        yield from _owned_property_blocks(
            component, location, ConfigLayer.COMPONENT, f"van component '{comp_name}'", f"components/{comp_index}"
        )

    for dep_index, deployment in enumerate(project_data.get("deployments", []) or []):
        if not isinstance(deployment, dict):
            continue
        dep_name = deployment.get("name", "(onbekend)")
        dep_location = f"deployment:{deployment.get('name')}"
        # Deployment-level service entries (clone state today, anything a service
        # declares tomorrow). ``$defs/deployment-service-config`` in the global schema is
        # deliberately open, so this walk is the only thing between a typo and a silently
        # ignored setting.
        for entry_index, entry in enumerate(deployment.get("services", []) or []):
            name = service_entry_name(entry)
            if name is None:
                continue
            yield ServiceConfigBlock(
                location=dep_location,
                where=f"in deployment '{dep_name}'",
                name=name,
                layer=ConfigLayer.DEPLOYMENT,
                config=service_entry_config(entry),
                from_version=service_entry_schema_version(entry),
                path=f"deployments/{dep_index}/services/{entry_index}/config",
            )

        # Deployment-component service entries. Two shapes live here: a dict keyed by
        # service name (``{publish-on-web: {config: ...}}``) and, for the storage
        # services, a list of per-mount records under that key. Both are walked, because
        # the global schema no longer guards this layer: opening up the deployment
        # envelope moved that job here.
        #
        # In that list shape one place carries MORE THAN ONE block of the same service,
        # one per mount, so the mount belongs in the location: the change rule pairs a
        # block with its previous version by (location, name), and without the mount two
        # mounts of the same service would share that key -- comparing one mount's size
        # with another's.
        for comp_index, component in enumerate(deployment.get("components", []) or []):
            if not isinstance(component, dict):
                continue
            comp_name = component.get("reference") or component.get("name", "(onbekend)")
            location = f"{dep_location}/component:{comp_name}"
            where = f"in component '{comp_name}' van deployment '{dep_name}'"
            base = f"deployments/{dep_index}/components/{comp_index}"
            services = component.get("services")
            if isinstance(services, dict):
                for name, body in services.items():
                    per_mount = isinstance(body, list)
                    for body_index, entry in enumerate(body if per_mount else [body]):
                        suffix = f"{name}/{body_index}" if per_mount else name
                        mount = entry.get("reference") if per_mount and isinstance(entry, dict) else None
                        yield ServiceConfigBlock(
                            location=location if mount is None else f"{location}/mount:{mount}",
                            where=where,
                            name=name,
                            layer=ConfigLayer.DEPLOYMENT_COMPONENT,
                            config=entry.get("config") if isinstance(entry, dict) else None,
                            from_version=service_entry_schema_version(entry),
                            path=f"{base}/services/{suffix}/config",
                        )
            elif isinstance(services, list):
                for entry_index, entry in enumerate(services):
                    name = service_entry_name(entry)
                    if name is None:
                        continue
                    yield ServiceConfigBlock(
                        location=location,
                        where=where,
                        name=name,
                        layer=ConfigLayer.DEPLOYMENT_COMPONENT,
                        config=service_entry_config(entry),
                        from_version=service_entry_schema_version(entry),
                        path=f"{base}/services/{entry_index}/config",
                    )
            yield from _owned_property_blocks(
                component,
                location,
                ConfigLayer.DEPLOYMENT_COMPONENT,
                f"van component '{comp_name}' in deployment '{dep_name}'",
                base,
            )


def _owned_property_blocks(
    component: dict[str, Any], location: str, layer: ConfigLayer, where: str, path: str
) -> Iterator[ServiceConfigBlock]:
    """The blocks of the SYSTEM services that own a plain component property (RC-25).

    ``user-env-vars`` and ``aliases`` are services whose config is a property of the
    component rather than a block in a ``services:`` list, so the walk above never sees
    them -- which is exactly why they went unvalidated until they were declared. The
    services name the property (``owned_property``) and the layers they carry it on
    (``config_editables``), so this loop names neither service.
    """
    for service in property_owning_services():
        key = service.owned_property
        if key is None or not service.config_editables(layer):
            continue
        yield ServiceConfigBlock(
            location=location,
            where=where,
            name=service.service_type.value,
            layer=layer,
            config=component.get(key),
            from_version=None,
            path=f"{path}/{key}",
            owned_property=key,
        )


def validate_service_configs(project_data: dict[str, Any]) -> None:
    """Validate every service's config against its provider's typed model (RC-5 A:
    the per-service config-validation chokepoint).

    Walks ``iter_service_config_blocks``, so it covers every layer a config can live at
    plus the component properties that SYSTEM services own. Services without a config
    block, or without a typed model, are skipped. Fails closed: raises
    ProjectIntegrityError on the first invalid service config.
    """
    view = Project(project_data)
    project_name = project_data.get("name", "(onbekend)")

    # The DEFINE side first: what a service stores under ``data`` (the attachments
    # catalog today). It was validated by nothing at all -- the config walk only ever
    # looked at ``config`` -- so a catalog entry with a missing filename or an id that
    # cannot become a volume name was committed and failed at deploy time.
    for name in ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", [])):
        data = service_entry_data(view.service_entry(name))
        if data is not None:
            _validate_one_data_block(name, data, ConfigLayer.PROJECT, "op projectniveau", project_name)

    for block in iter_service_config_blocks(project_data):
        if block.config is None:
            continue  # bare reference / no config to validate
        if block.owned_property is not None:
            _validate_owned_property_block(block, project_name)
        else:
            _validate_one_config(block, project_name)


def find_plaintext_service_config_violations(project_data: dict[str, Any]) -> list[str]:
    """Paths in a SERVICE config that must hold an AGE-encrypted value but do not.

    The counterpart of ``find_plaintext_secret_violations`` for the half of a project file
    that ``project_v2.json`` does not describe, because a service's config shape is owned
    by that service's model. Detection comes from the schema (a ``pattern`` carrying the
    AGE marker), never from a field list, and judges the block as it is STORED. The
    owned-property blocks are component properties, which ``project_v2.json`` does describe.
    """
    violations: list[str] = []
    for block in iter_service_config_blocks(project_data):
        if block.config is None or block.owned_property is not None:
            continue
        try:
            service_type = ServiceType(block.name)
        except ValueError:
            continue  # unknown service name, other validation handles it
        model = get_service(service_type).config_model_for(block.layer)
        if model is None:
            continue  # service has no model at this layer, so nothing declares a secret
        violations.extend(age_pattern_violations(_model_validator(model), block.config, prefix=block.path))
    return sorted(set(violations))


@cache
def _model_validator(model: type[BaseModel]) -> Draft202012Validator:
    """The JSON-schema validator for one config model, built once per model."""
    return Draft202012Validator(model.model_json_schema())


def _validate_owned_property_block(block: ServiceConfigBlock, project_name: str) -> None:
    """Validate one owned-property block from the walk: its model AND its latitude.

    Both, because the walk hands these blocks to the change check as well; judging them
    on their change but not on their bounds is the same divergence the shared walk closed,
    only the other way around. Neither of the two names the value: see
    ``_setting_refusal`` for the latitude check.
    """
    service = get_service(ServiceType(block.name))
    model = service.config_model
    # property_owning_services() filters on owned_property, and a service that owns one is
    # always modelled -- but this is a fail-closed validation path, so it narrows
    # explicitly instead of leaning on an assert (which `python -O` strips, turning the
    # guarantee into a silent skip). The latitude check below does not depend on a model.
    if model is not None:
        _validate_owned_property(service, model, block.config, block.where, project_name)
    _check_declared_settings(service, block, project_name)


def _validate_owned_property(service: Service, model: type[BaseModel], raw: Any, where: str, project_name: str) -> None:
    """Validate one owned-property value against its service's model; fail closed.

    The model is passed in rather than read off the service again, so the narrowing the
    caller already did is carried in the signature instead of re-asserted here.

    The message names only the validators' own reasons, never the value. The properties
    these services own hold secrets (``user-env-vars`` is the component's own environment,
    a value may be a password), and this message is both logged at WARNING and returned to
    the caller -- so ``str(e)`` would put a pasted secret from an unparseable plaintext
    value in the central OPI log and in an HTTP response. ``e.errors()[*]["msg"]`` carries
    the reason without pydantic's ``input_value``, and the chain is dropped (``from None``)
    because the ValidationError itself still holds the input for any handler that logs a
    traceback.
    """
    try:
        model.model_validate(raw)
    except ValidationError as e:
        reasons = validation_reasons(e)
        raise ProjectIntegrityError(
            f"Project '{project_name}': '{service.owned_property}' {where} is ongeldig: {reasons}."
        ) from None


def validate_component_references(project_data: dict, components: list, context: str = "deployment") -> dict[str, Any]:
    """
    Validate that all component references exist in the project.

    Args:
        project_data: The project data containing component definitions
        components: List of ComponentReference objects or dicts with 'reference' key
        context: Context for error messages (e.g. "deployment", "update")

    Returns:
        Dict with validation result: {"success": bool, "error": str | None, "invalid_references": list | None}
    """
    project_components = project_data.get("components", [])
    component_names = {comp.get("name") for comp in project_components}
    invalid_references = []

    for component in components:
        # Handle both ComponentReference objects and dict format
        reference = getattr(component, "reference", None) or component.get("reference")

        if reference not in component_names:
            invalid_references.append(reference)

    if invalid_references:
        available_components = list(component_names) if component_names else ["none"]
        project_name = project_data.get("name", "unknown")
        error_msg = f"Invalid component references in {context} for project '{project_name}': {invalid_references}. Available components: {available_components}"
        logger.warning(error_msg)
        return {"success": False, "error": error_msg, "invalid_references": invalid_references}

    return {"success": True, "error": None, "invalid_references": None}


def _validate_services_listed_once(services: Any, project_name: str, where: str) -> None:
    """A services list may name each service at most once.

    The list is a selection set keyed by service name, so a repeat has no meaning: a
    second entry either says the same thing or silently contradicts the first, and
    every reader (config lookup, provisioning, manifest generation) sees only one of
    them. A hand-edited project file can still contain one, which is why this is a
    check that rejects rather than something that quietly collapses the list.
    """
    if not isinstance(services, list):
        return
    seen: set[str] = set()
    for entry in services:
        name = service_entry_name(entry)
        if name is None:
            continue
        if name in seen:
            raise ProjectIntegrityError(
                f"Project '{project_name}': service '{name}' staat meerdere keren in de services-lijst op {where}"
            )
        seen.add(name)


def _project_context_kwargs(provider_class: type, project_data: dict[str, Any]) -> dict[str, Any]:
    """The constructor arguments *provider_class* accepts out of the project context.

    The same trick the form bridge uses (``_filter_provider_kwargs``): a provider declares
    what it needs as ``__init__`` parameters and the caller matches by name, so a provider
    with a fixed list takes no arguments and one that reads the project takes ``yaml_data``.

    ``current_value`` is deliberately NOT offered. A provider given one keeps an unknown
    stored value as an option flagged "(bestaat niet meer)", which is what stops a form
    save from silently dropping it -- and would make every value valid here.
    """
    import inspect

    accepted = set(inspect.signature(provider_class.__init__).parameters) - {"self"}
    return {"yaml_data": project_data} if "yaml_data" in accepted else {}


def validate_declared_choices(project_data: dict[str, Any]) -> list[str]:
    """Values that reference something in this project which is not in this project.

    The sibling of ``collect_config_advice`` and deliberately the other verdict. That one
    asks whether a field is FILLED given a setting elsewhere and warns; this one asks
    whether the value that IS there exists, and refuses. A realm role that no keycloak
    config defines is not a choice with a downside, it is a typo: keycloak assigns
    nothing on redemption (``assign_realm_roles_to_user`` reports it under ``not_found``
    and moves on), so the invited user arrives without the role and, under
    ``restrict-access``, without access.

    The set of valid values is not restated here. It comes from the field's own
    ``values_provider`` -- the same provider that fills the form's select and the same one
    ``x-choices-source`` names in the OpenAPI document, so a caller is judged against
    exactly the list they were told to read.

    Opt-in per field with ``Editable.values_must_exist``, because an options list is
    usually a menu rather than a closed set (see that flag). Only fields that carry it are
    looked at, and only the layers the service declares config on.
    """
    from opi.forms.editables.service_path import expand_wildcard_path
    from opi.forms.visualizers.providers import PROVIDER_REGISTRY, UNDECLARED_SOURCE, OptionsSource

    def walk(editables: list[Editable]) -> list[Editable]:
        found: list[Editable] = []
        for editable in editables:
            found.append(editable)
            found.extend(walk(editable.children or []))
        return found

    errors: list[str] = []
    seen_paths: set[str] = set()
    for service in SERVICES.values():
        for layer in service.config_layers():
            for editable in walk(service.config_editables(layer)):
                if not editable.values_must_exist or not editable.values_provider:
                    continue
                if editable.yaml_path in seen_paths:
                    continue  # the same editable can be declared on more than one layer
                seen_paths.add(editable.yaml_path)
                provider_class = PROVIDER_REGISTRY.get(editable.values_provider)
                if provider_class is None:
                    logger.warning(
                        "Onbekende values_provider %r op %s; waarden niet gecontroleerd",
                        editable.values_provider,
                        editable.yaml_path,
                    )
                    continue
                provider = provider_class(**_project_context_kwargs(provider_class, project_data))
                allowed = {str(option.get("value")) for option in provider.get_options()}
                offered = sorted(option for option in allowed if option)
                if not offered:
                    # The source is empty, so there is nothing to be measured against. This
                    # is not "everything is wrong": it is a project whose values come from
                    # somewhere this provider cannot see -- the four pre-service invite
                    # files name roles of a realm nobody configured through ZAD. Refusing
                    # there would claim knowledge we do not have, and would block the next
                    # edit of a project over a value this release did not introduce. The
                    # case that matters is never empty: with restrict-access on there is
                    # always at least the wall role.
                    continue
                source = getattr(provider_class, "options_source", UNDECLARED_SOURCE)
                what = f"{source.description} " if isinstance(source, OptionsSource) else ""
                for path, value in expand_wildcard_path(project_data, editable.yaml_path):
                    if value is None or str(value) in allowed:
                        continue
                    errors.append(
                        f"'{value}' op {path} bestaat niet in dit project. {what}Nu beschikbaar: {', '.join(offered)}."
                    )
    return errors


def validate_database_schema_names(project_data: dict[str, Any]) -> list[str]:
    """The composed schema names of every extra schema, against every deployment (RC-59).

    ``{project}_{deployment}_{postfix}`` has to fit in PostgreSQL's 63 characters, and how
    much room the postfix has depends on the project and deployment names -- so this cannot
    be a field rule and cannot be decided when the postfix is typed.

    ``UniqueSchemaEnforcer`` already ran it, but only when the *schema list* was being
    saved, and only against the deployments that existed at that moment. That leaves the
    real hole: a postfix that fits today stops fitting the moment a deployment with a
    longer name is added, and nothing on that road looks at schemas. The failure then
    surfaced at rollout, as a ``ValueError`` out of ``generate_extra_database_schema``,
    long after the change that caused it.

    Running it here, in the structural validation every save passes through, closes that:
    adding the deployment is refused, with a message that names both the deployment and
    the postfix that no longer fits.

    Schemas marked for deletion are skipped -- they are on their way out and must not
    block a save.
    """
    errors: list[str] = []
    project_name = project_data.get("name") or ""
    deployment_names = [
        name for d in (project_data.get("deployments") or []) if isinstance(d, dict) and (name := d.get("name"))
    ]
    if not deployment_names:
        return errors

    for entry in get_postgres_schemas(project_data):
        postfix = entry.get("postfix")
        if not postfix:
            continue
        for deployment_name in deployment_names:
            try:
                generate_extra_database_schema(project_name, deployment_name, postfix)
            except ValueError:
                errors.append(
                    f"schema '{postfix}' levert voor deployment '{deployment_name}' een naam op die langer is "
                    f"dan de 63 tekens die PostgreSQL toestaat. Kies een kortere postfix of een kortere "
                    f"deploymentnaam."
                )
    return errors


def _platform_registry_repo() -> str | None:
    """The one repository the platform's image-push endpoint writes into, or None.

    Everything a project pushes lands in ``{REGISTRY_URL}/{REGISTRY_ORG}``; ownership
    lives in the tag (see ``build_registry_tag``). Returns None when no registry is
    configured, which is the case on clusters without the image-push feature.
    """
    if not settings.REGISTRY_URL or not settings.REGISTRY_ORG:
        return None
    return f"{settings.REGISTRY_URL}/{settings.REGISTRY_ORG}"


def validate_platform_registry_image_ownership(project_data: dict[str, Any]) -> list[str]:
    """Reject deployment images that point at another project's tag in the shared registry.

    Pinning the push side stops a project from writing another's image; without this
    a project could still READ one, by naming the other's tag as its own deployment
    image. Only references into the platform's own registry repository are judged --
    an image from ghcr.io, Docker Hub or a project's own registry is nobody's business
    here, and is left alone.

    The reference is normalized before it is judged, because one repository has more
    than one valid spelling (uppercase host, an explicit ``:443``). A digest reference
    into the platform repository is refused outright: ownership lives in the tag, and
    a digest names an image in the shared repository without naming its owner.

    Tags from before ownership pinning carry no owner prefix and stay usable, so
    deployments that already run keep running.
    """
    platform_repo = _platform_registry_repo()
    if platform_repo is None:
        return []
    platform_repo = normalize_registry_repo(platform_repo)

    project_name = project_data.get("name", "")
    errors: list[str] = []
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        for component in deployment.get("components", []) or []:
            if not isinstance(component, dict):
                continue
            image = component.get("image")
            if not isinstance(image, str):
                continue
            repo, registry_tag, has_digest = split_image_reference(image)
            if normalize_registry_repo(repo) != platform_repo:
                continue
            where = f"deployment '{deployment.get('name')}' component '{component.get('reference')}'"
            if has_digest:
                errors.append(
                    f"{where} verwijst met '{image}' naar de gedeelde platformregistry met een digest. "
                    f"Daar staat niet in van wie de image is, dus verwijs naar je eigen tag "
                    f"('{project_name}_...') in plaats van naar een digest"
                )
                continue
            owner = registry_tag_owner(registry_tag) if registry_tag is not None else None
            if owner is not None and owner != project_name:
                errors.append(
                    f"{where} verwijst met '{image}' naar een image in de gedeelde platformregistry "
                    f"die van project '{owner}' is. "
                    f"Je kunt daar alleen images gebruiken die je zelf gepusht hebt"
                )
    return errors


def validate_service_availability(project_data: dict[str, Any]) -> list[str]:
    """Services this project selected that its deployments' clusters cannot deliver.

    Asked of each service (``Service.available_on_cluster``), so no cluster name and no
    service name appears in this module. Measured per DEPLOYMENT cluster rather than
    against the managing cluster: that is the cluster the pods will actually run on, and
    it keeps the verdict the same file-in, file-out no matter which OPI instance reads
    the project.

    This is the refusal that counts. Leaving an unavailable service out of the wizard's
    cards hides it from one of three roads; the API and a hand-written project file never
    pass a card at all.
    """
    selected = ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", []) or [])
    if not selected:
        return []

    errors: list[str] = []
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        cluster = deployment.get("cluster")
        if not cluster or cluster not in CLUSTER_CONFIG:
            continue
        for name in selected:
            try:
                service_type = ServiceType(name)
            except ValueError:
                # An unknown service name is another check's verdict, not this one's.
                continue
            service = SERVICES.get(service_type)
            if service is None or service.available_on_cluster(cluster):
                continue
            errors.append(
                f"deployment '{deployment.get('name')}' draait op cluster '{cluster}', en daar is de dienst "
                f"'{name}' niet beschikbaar"
            )
    return errors


def validate_service_setting_changes(previous: dict[str, Any], project_data: dict[str, Any]) -> None:
    """Judge what this save CHANGES about a service's declared settings (RC-168).

    Some fields cannot move both ways: a PVC cannot shrink, so accepting a smaller
    ``storage`` because it happens to sit between the declared bounds means a value that
    then silently does nothing or wedges the rollout. That is a rule about a change, not
    about a value, so it needs the version being replaced -- which is why it lives here
    and not in the per-block walk.

    Blocks are matched by location, so a newly added component or deployment has nothing
    to be compared against and is only judged on its bounds. Comparing per block is the
    same as comparing the EFFECTIVE value because a setting that judges a change may name
    only one layer -- ``ConfigSetting.__post_init__`` refuses the declaration otherwise --
    so the block at that PLACE is the only one ``resolve_setting`` would read for that
    field. Which is why the location goes down to the mount where a service keeps a
    record per mount: there one layer still carries several blocks under one component,
    each with its own effective value, and one key for all of them would both let a
    shrink through and refuse a file in which nothing changed at all.

    A field a version does not mention was standing on the service default, and that is
    what it is compared with -- on both sides, so a reduction cannot be smuggled in by
    leaving the field, or the whole config block, out. That reading holds all the way up: the previous version is
    looked up by its KEY, so a service referenced BARE there (no config block at all) is
    a version standing on the defaults, not an absence.

    A service that is no longer referenced at that place is not in the walk and so is
    judged on nothing: dropping a service is a removal, not a reduction of its fields.

    Fails closed: raises ProjectIntegrityError on the first refused change.
    """
    project_name = project_data.get("name", "(onbekend)")
    before = {(block.location, block.name): block.config for block in iter_service_config_blocks(previous)}
    for block in iter_service_config_blocks(project_data):
        key = (block.location, block.name)
        if key not in before:
            continue  # the service was not there before: an addition, no change to judge
        # Asked of the KEY, not of the value. A service referenced bare (config None) is
        # standing on the service defaults, which is exactly the version a reduction has
        # to be measured against -- reading the value here would make "bare" and "not
        # there" the same answer again, and leaving the config block out the way around
        # the rule.
        old_config = before[key]
        try:
            service_type = ServiceType(block.name)
        except ValueError:
            continue  # unknown service name -- other validation handles it
        try:
            check_setting_changes(get_service(service_type).config_settings(), old_config, block.config, block.layer)
        except SettingError as e:
            raise ProjectIntegrityError(
                f"Project '{project_name}': configuratie van service '{block.name}' kan niet zo worden gewijzigd: "
                f"{_setting_refusal(e, block)}"
            ) from e


async def validate_project_structure(project_data: dict[str, Any], *, previous: dict[str, Any] | None = None) -> None:
    """Validate cross-field structural integrity of a complete project dict.

    Runs the reference/uniqueness/path/root/domain checks against the final merged
    dict so they hold no matter which caller produced it. Raises
    ProjectIntegrityError on the first violation; fails closed.

    ``previous`` is the version this one replaces, when the caller has it. Only the
    rules that judge a CHANGE rather than a value need it (see
    ``validate_service_setting_changes``); everything else reads ``project_data`` alone,
    so a caller without a previous version -- a create, a replay -- loses nothing else.
    """
    project_name = project_data.get("name", "(onbekend)")
    components = project_data.get("components", []) or []
    deployments = project_data.get("deployments", []) or []

    # Component names unique
    seen_components: set[str] = set()
    for comp in components:
        if not isinstance(comp, dict):
            continue
        cname = comp.get("name")
        if not isinstance(cname, str):
            raise ProjectIntegrityError(f"Project '{project_name}': een component zonder naam")
        if cname in seen_components:
            raise ProjectIntegrityError(f"Project '{project_name}': component '{cname}' is meervoudig gedefinieerd")
        seen_components.add(cname)

    # A services list is a selection set: each service at most once
    _validate_services_listed_once(project_data.get("services"), project_name, "projectniveau")
    for comp in components:
        if isinstance(comp, dict):
            _validate_services_listed_once(comp.get("services"), project_name, f"component '{comp.get('name')}'")
    for dep in deployments:
        if not isinstance(dep, dict):
            continue
        _validate_services_listed_once(dep.get("services"), project_name, f"deployment '{dep.get('name')}'")
        for ref in dep.get("components", []) or []:
            if isinstance(ref, dict):
                _validate_services_listed_once(
                    ref.get("services"),
                    project_name,
                    f"deployment '{dep.get('name')}' component '{ref.get('reference')}'",
                )

    project_service_names = set(
        ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", []))
    )
    component_by_name = {c.get("name"): c for c in components if isinstance(c, dict)}

    # Component service references resolve to a project-level service
    for comp in components:
        if not isinstance(comp, dict):
            continue
        comp_service_names = ServiceAdapter.extract_service_names_from_project_services(comp.get("services", []))
        invalid_services = [s for s in comp_service_names if s not in project_service_names]
        if invalid_services:
            raise ProjectIntegrityError(
                f"Project '{project_name}': component '{comp.get('name')}' verwijst naar services die niet op "
                f"projectniveau bestaan: {invalid_services}"
            )

    # Deployment names unique
    seen_deployments: set[str] = set()
    for index, dep in enumerate(deployments):
        if not isinstance(dep, dict):
            continue
        dep_name = dep.get("name")
        if not isinstance(dep_name, str):
            raise ProjectIntegrityError(f"Project '{project_name}': een deployment zonder naam")
        if dep_name in seen_deployments:
            raise ProjectIntegrityError(f"Project '{project_name}': deployment '{dep_name}' is meervoudig gedefinieerd")
        if dep_name in RESERVED_DEPLOYMENT_NAMES:
            raise ProjectIntegrityError(
                f"Project '{project_name}': '{dep_name}' is een gereserveerde deploymentnaam en kan niet "
                f"gebruikt worden. Het platform gebruikt hem zelf; kies een andere naam."
            )
        seen_deployments.add(dep_name)

        refs = dep.get("components", []) or []

        # All component references resolve to a defined component
        reference_result = validate_component_references(project_data, refs, "deployment")
        if not reference_result["success"]:
            raise ProjectIntegrityError(reference_result["error"])

        # Root component constraints
        root_ref = get_domain_setting(dep, DomainSetting.ROOT_COMPONENT)
        if root_ref:
            ref_names = [name for r in refs if isinstance(r, dict) and (name := r.get("reference"))]
            try:
                validate_root_component(root_ref, ref_names, get_domain_setting(dep, DomainSetting.DOMAIN_FORMAT))
            except ComponentValidationError as e:
                raise ProjectIntegrityError(str(e)) from e

        # Hard domain-config violations. A FieldWarning (e.g. an unapproved
        # custom domain) is non-fatal: the UI handles it via domain-request
        # entries, so only a ValueError/FieldError is a structural rejection.
        # ``denied_blocks=False``: a revoked approval on a domain a deployment already
        # uses must be saveable, otherwise the approver cannot record their own verdict.
        # The revocation takes effect at publication (apply_domain_approval_fallback),
        # not by refusing the write.
        try:
            await DomainConfigEnforcer(deployment_index=index, denied_blocks=False).enforce(
                project_data, {"project_name": project_name}
            )
        except FieldWarning:
            pass
        except ValueError as e:
            raise ProjectIntegrityError(str(e)) from e

    # Attachment references must resolve to a catalog entry, so an unknown id
    # is rejected at save time instead of failing later at deploy/resolve time.
    attachment_errors = validate_attachment_references(project_data)
    if attachment_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(attachment_errors)}")

    # Attachment couplings must be structurally valid: no reference coupled
    # twice, no empty delivery target, no colliding path/env-var. The base
    # component 'services' list is not covered by the JSON schema, so this is
    # the only place a duplicate reference with an empty path is rejected
    # before it can be committed.
    coupling_errors = validate_attachment_couplings(project_data)
    if coupling_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(coupling_errors)}")

    # Extra database schemas: the composed name has to fit for EVERY deployment, so this
    # belongs here rather than only on the road that edits the schema list -- adding a
    # deployment is the other way a valid schema name becomes an impossible one.
    schema_errors = validate_database_schema_names(project_data)
    if schema_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(schema_errors)}")

    # A service is only usable where the cluster can deliver it. Here rather than at the
    # form field because a project reaches this point from the wizard, the API and a
    # hand-edited file alike.
    availability_errors = validate_service_availability(project_data)
    if availability_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(availability_errors)}")

    # A deployment may not point at another project's tag in the shared platform registry.
    # The platform's own registry is not a service, so this rule has no service to live in.
    registry_errors = validate_platform_registry_image_ownership(project_data)
    if registry_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(registry_errors)}")

    # Every service's own rules on the whole project (``Service.validate_project``).
    service_errors: list[str] = []
    for service in project_validating_services():
        service_errors.extend(service.validate_project(project_data))
    if service_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(service_errors)}")

    # Values that point at something in this project: a realm role an invite hands out
    # has to be a realm role the keycloak config defines, or nobody gets it. Per-service
    # model validation cannot see this -- it judges one config block in isolation, and
    # both halves of the reference live in different services.
    choice_errors = validate_declared_choices(project_data)
    if choice_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(choice_errors)}")

    # Per-service typed config validation (RC-5 A). Runs last: the envelope and
    # cross-field structure are valid by here, so this only judges the config values.
    validate_service_configs(project_data)

    # And what the save CHANGES about those values, for the fields a service declared
    # can only move one way. Only when the caller handed over the version being replaced.
    if previous is not None:
        validate_service_setting_changes(previous, project_data)
