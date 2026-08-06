"""Structural validation of a complete project dict.

Extracted from ProjectManager so the ProjectStore can run the SAME checks on the
final state of a mutation, before anything is written or committed. Keeping one
implementation means there is no "less validated" write path: ProjectManager and
ProjectStore both call into here.

Runs AFTER json-schema validation (opi.core.project_schema.validate_project_schema)
and BEFORE any write or commit. Fails closed on the first violation.
"""

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning
from opi.handlers.project_file_handler import validate_attachment_couplings, validate_attachment_references
from opi.services import ServiceAdapter
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.project import Project
from opi.services.registry import get_service, property_owning_services
from opi.services.services import service_entry_config, service_entry_name, service_entry_schema_version
from opi.services.services_enums import ServiceType
from opi.utils.project_utils import ComponentValidationError, validate_component_paths, validate_root_component

logger = logging.getLogger(__name__)


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


def _validate_one_config(
    name: str, raw: Any, layer: ConfigLayer, where: str, project_name: str, from_version: str | None = None
) -> None:
    """Validate one service config block against its provider's typed model.

    Shared by the project-level and component-level walks. Skips services that are
    unknown or take no typed config. ``from_version`` is the entry's stamped
    ``schema-version``, threaded through so the provider migrates an older config
    block forward before validating (None = current version). Fails closed: raises
    ProjectIntegrityError, with the service's own accepted-field list
    (config_api_fields / config_editables) appended so the message tells the user
    which keys the service accepts.
    """
    try:
        service_type = ServiceType(name)
    except ValueError:
        return  # unknown service name -- other validation handles it
    provider = get_service(service_type)
    model = provider.config_model_for(layer)
    if model is None:
        return  # service takes no typed config at this layer
    try:
        if model is provider.config_model:
            provider.validate_config(raw, from_version=from_version)
        else:
            # A layer-specific model (per-mount clone state). OPI writes it, so there is no
            # stamped version to migrate from; validate the shape directly.
            model.model_validate(raw)
    except ValidationError as e:
        accepted = _accepted_config_fields(provider, layer)
        hint = f" Geaccepteerde velden: {', '.join(accepted)}." if accepted else ""
        raise ProjectIntegrityError(
            f"Project '{project_name}': configuratie van service '{name}' {where} is ongeldig: {e}.{hint}"
        ) from e


def validate_service_configs(project_data: dict[str, Any]) -> None:
    """Validate every service's config against its provider's typed model (RC-5 A:
    the per-service config-validation chokepoint).

    Covers all three layers a config can live at: project-level service definitions
    (keycloak, namespace-postgres, auth-wall), component-level service references
    (persistent-storage / temp-storage mounts, metrics-scraper port/path) and
    deployment-level entries (clone state today). Services without a config block, or
    without a typed model, are skipped. Fails closed: raises ProjectIntegrityError on the
    first invalid service config.
    """
    view = Project(project_data)
    project_name = project_data.get("name", "(onbekend)")

    # Project-level service definitions.
    for name in ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", [])):
        raw = view.service_config(name)
        if raw is None:
            continue  # bare service / no project-level config to validate
        from_version = service_entry_schema_version(view.service_entry(name))
        _validate_one_config(name, raw, ConfigLayer.PROJECT, "op projectniveau", project_name, from_version)

    # Component-level service references (storage mounts, metrics port/path). Their
    # config lives on the component's service entry, not at project level, so the
    # project-level walk above never sees it.
    for component in project_data.get("components", []) or []:
        if not isinstance(component, dict):
            continue
        comp_name = component.get("name", "(onbekend)")
        for entry in component.get("services", []) or []:
            name = service_entry_name(entry)
            config = service_entry_config(entry)
            if name is None or config is None:
                continue  # bare reference / no config to validate
            from_version = service_entry_schema_version(entry)
            _validate_one_config(
                name, config, ConfigLayer.COMPONENT, f"in component '{comp_name}'", project_name, from_version
            )

    # Deployment-level service entries (clone state today, anything a service declares
    # tomorrow). ``$defs/deployment-service-config`` in the global schema is deliberately
    # open, so this walk is the only thing between a typo and a silently ignored setting.
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        dep_name = deployment.get("name", "(onbekend)")
        for entry in deployment.get("services", []) or []:
            name = service_entry_name(entry)
            config = service_entry_config(entry)
            if name is None or config is None:
                continue  # bare reference / no config to validate
            from_version = service_entry_schema_version(entry)
            _validate_one_config(
                name, config, ConfigLayer.DEPLOYMENT, f"in deployment '{dep_name}'", project_name, from_version
            )

    # Deployment-component service entries. Two shapes live here: a dict keyed by service
    # name (``{publish-on-web: {config: ...}}``) and, for the storage services, a list of
    # per-mount records under that key. Both are walked, because the global schema no longer
    # guards this layer: opening up the deployment envelope moved that job here.
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        dep_name = deployment.get("name", "(onbekend)")
        for component in deployment.get("components", []) or []:
            if not isinstance(component, dict):
                continue
            comp_name = component.get("reference") or component.get("name", "(onbekend)")
            where = f"in component '{comp_name}' van deployment '{dep_name}'"
            services = component.get("services")
            if isinstance(services, dict):
                for name, body in services.items():
                    for entry in body if isinstance(body, list) else [body]:
                        config = entry.get("config") if isinstance(entry, dict) else None
                        if config is None:
                            continue
                        _validate_one_config(
                            name,
                            config,
                            ConfigLayer.DEPLOYMENT_COMPONENT,
                            where,
                            project_name,
                            service_entry_schema_version(entry),
                        )
            elif isinstance(services, list):
                for entry in services:
                    name = service_entry_name(entry)
                    config = service_entry_config(entry)
                    if name is None or config is None:
                        continue
                    _validate_one_config(
                        name,
                        config,
                        ConfigLayer.DEPLOYMENT_COMPONENT,
                        where,
                        project_name,
                        service_entry_schema_version(entry),
                    )

    _validate_owned_properties(project_data, project_name)


def _validate_owned_properties(project_data: dict[str, Any], project_name: str) -> None:
    """Validate the plain component properties that SYSTEM services own (RC-25).

    ``user-env-vars`` and ``aliases`` are services whose config is a property of the
    component rather than a block in a ``services:`` list, so the walks above never see
    them -- which is exactly why they went unvalidated until now. The services declare
    the property (``owned_property``) and the layers they carry it on
    (``config_editables``), so this loop names neither service.
    """
    for service in property_owning_services():
        key, model = service.owned_property, service.config_model
        if key is None or model is None:
            # property_owning_services() filters on owned_property, and a service that owns
            # one is always modelled -- but this walk is a fail-closed validation path, so
            # it narrows explicitly instead of leaning on an assert (which `python -O`
            # strips, turning the guarantee into a silent skip).
            continue
        if service.config_editables(ConfigLayer.COMPONENT):
            for component in project_data.get("components", []) or []:
                if isinstance(component, dict) and component.get(key) is not None:
                    _validate_owned_property(
                        service,
                        model,
                        component[key],
                        f"van component '{component.get('name', '(onbekend)')}'",
                        project_name,
                    )
        if not service.config_editables(ConfigLayer.DEPLOYMENT_COMPONENT):
            continue
        for deployment in project_data.get("deployments", []) or []:
            if not isinstance(deployment, dict):
                continue
            dep_name = deployment.get("name", "(onbekend)")
            for component in deployment.get("components", []) or []:
                if isinstance(component, dict) and component.get(key) is not None:
                    comp_name = component.get("reference") or component.get("name", "(onbekend)")
                    _validate_owned_property(
                        service,
                        model,
                        component[key],
                        f"van component '{comp_name}' in deployment '{dep_name}'",
                        project_name,
                    )


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
        reasons = "; ".join(error["msg"] for error in e.errors()) or "waarde voldoet niet aan het model"
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
        logger.error(error_msg)
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


async def validate_project_structure(project_data: dict[str, Any]) -> None:
    """Validate cross-field structural integrity of a complete project dict.

    Runs the reference/uniqueness/path/root/domain checks against the final merged
    dict so they hold no matter which caller produced it. Raises
    ProjectIntegrityError on the first violation; fails closed.
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
        seen_deployments.add(dep_name)

        refs = dep.get("components", []) or []
        domain_mode = dep.get("domain-mode", "component-specific")

        # All component references resolve to a defined component
        reference_result = validate_component_references(project_data, refs, "deployment")
        if not reference_result["success"]:
            raise ProjectIntegrityError(reference_result["error"])

        # Ingress path uniqueness within the deployment
        paths = []
        for ref in refs:
            ref_name = ref.get("reference") if isinstance(ref, dict) else None
            comp = component_by_name.get(ref_name)
            if comp:
                paths.append(comp.get("path", "/"))
        try:
            validate_component_paths(paths, domain_mode)
        except ComponentValidationError as e:
            raise ProjectIntegrityError(str(e)) from e

        # Root component constraints
        root_ref = dep.get("root-component")
        if root_ref:
            ref_names = [name for r in refs if isinstance(r, dict) and (name := r.get("reference"))]
            try:
                validate_root_component(root_ref, ref_names, domain_mode, dep.get("domain-format"))
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

    # Per-service typed config validation (RC-5 A). Runs last: the envelope and
    # cross-field structure are valid by here, so this only judges the config values.
    validate_service_configs(project_data)
