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

from pydantic import ValidationError

from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning
from opi.handlers.project_file_handler import validate_attachment_couplings, validate_attachment_references
from opi.services import ServiceAdapter
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.project import Project
from opi.services.registry import get_service
from opi.services.services import service_entry_config, service_entry_name
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


def _validate_one_config(name: str, raw: Any, layer: ConfigLayer, where: str, project_name: str) -> None:
    """Validate one service config block against its provider's typed model.

    Shared by the project-level and component-level walks. Skips services that are
    unknown or take no typed config. Fails closed: raises ProjectIntegrityError, with
    the service's own accepted-field list (config_api_fields / config_editables)
    appended so the message tells the user which keys the service accepts.
    """
    try:
        service_type = ServiceType(name)
    except ValueError:
        return  # unknown service name -- other validation handles it
    provider = get_service(service_type)
    if provider.config_model is None:
        return  # service takes no typed config
    try:
        provider.validate_config(raw)
    except ValidationError as e:
        accepted = _accepted_config_fields(provider, layer)
        hint = f" Geaccepteerde velden: {', '.join(accepted)}." if accepted else ""
        raise ProjectIntegrityError(
            f"Project '{project_name}': configuratie van service '{name}' {where} is ongeldig: {e}.{hint}"
        ) from e


def validate_service_configs(project_data: dict[str, Any]) -> None:
    """Validate every service's config against its provider's typed model (RC-5 A:
    the per-service config-validation chokepoint).

    Covers BOTH layers a config can live at: project-level service definitions
    (keycloak, namespace-postgres, auth-wall) and component-level service references
    (persistent-storage / temp-storage mounts, metrics-scraper port/path). Services
    without a config block, or without a typed model, are skipped. Fails closed:
    raises ProjectIntegrityError on the first invalid service config.
    """
    view = Project(project_data)
    project_name = project_data.get("name", "(onbekend)")

    # Project-level service definitions.
    for name in ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", [])):
        raw = view.service_config(name)
        if raw is None:
            continue  # bare service / no project-level config to validate
        _validate_one_config(name, raw, ConfigLayer.PROJECT, "op projectniveau", project_name)

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
            _validate_one_config(name, config, ConfigLayer.COMPONENT, f"in component '{comp_name}'", project_name)


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
        if cname in seen_components:
            raise ProjectIntegrityError(f"Project '{project_name}': component '{cname}' is meervoudig gedefinieerd")
        seen_components.add(cname)

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
            ref_names = [r.get("reference") for r in refs if isinstance(r, dict) and r.get("reference")]
            try:
                validate_root_component(root_ref, ref_names, domain_mode, dep.get("domain-format"))
            except ComponentValidationError as e:
                raise ProjectIntegrityError(str(e)) from e

        # Hard domain-config violations. A FieldWarning (e.g. an unapproved
        # custom domain) is non-fatal: the UI handles it via domain-request
        # entries, so only a ValueError/FieldError is a structural rejection.
        try:
            await DomainConfigEnforcer(deployment_index=index).enforce(project_data, {"project_name": project_name})
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
