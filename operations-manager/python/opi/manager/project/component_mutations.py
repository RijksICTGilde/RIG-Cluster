"""Writing component definitions into the project file, and their use in deployments."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opi.services import ServiceAdapter, ServiceValidationError
from opi.services.approvals import ensure_approval_requests
from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    get_domain_setting,
    set_domain_setting,
)
from opi.services.project_file_handler import ComponentInUseError, remove_component
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.services.project_utils import (
    ComponentValidationError,
    build_component_config,
    normalize_container_image,
    validate_root_component,
)
from opi.services.services import service_entry_name
from opi.utils.age import get_project_public_key

if TYPE_CHECKING:
    from opi.manager.project.access import ProjectFileAccess

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ComponentAttachment:
    """What an add writes into each target deployment."""

    name: str
    image: str
    root: bool


def _refuse_new_component(
    project_data: dict[str, Any],
    name: str,
    project_name: str,
    deployment_names: list[str],
    *,
    root: bool,
) -> dict[str, Any] | None:
    """Why this component cannot be added, or None when it can.

    All three checks run before anything is written, so a refusal leaves the project file
    exactly as it was.
    """
    for component in project_data.get("components", []):
        if component.get("name") == name:
            return {
                "success": False,
                "error": f"Component '{name}' already exists in project '{project_name}'",
                "error_type": "duplicate_component",
            }

    existing_deployments = project_data.get("deployments", [])
    known = {d.get("name") for d in existing_deployments}
    unknown = [d for d in deployment_names if d not in known]
    if unknown:
        return {
            "success": False,
            "error": f"Deployments not found: {unknown}",
            "error_type": "invalid_deployments",
        }

    if not root:
        return None

    for deployment in existing_deployments:
        if deployment.get("name") not in deployment_names:
            continue
        references = [c.get("reference") for c in deployment.get("components", []) if c.get("reference")]
        try:
            validate_root_component(
                name, [*references, name], get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT)
            )
        except ComponentValidationError as e:
            return {"success": False, "error": str(e), "error_type": "validation_error"}

    return None


def _add_commit_message(name: str, project_name: str, deployments_updated: list[str]) -> str:
    """Name in the commit which deployments the new component was attached to, if any."""
    message = f"Add component '{name}' to project '{project_name}'"
    if deployments_updated:
        message += f" (deployments: {', '.join(deployments_updated)})"
    return message


def _attach_component_to_deployments(
    existing_deployments: list[dict[str, Any]], deployment_names: list[str], attachment: _ComponentAttachment
) -> list[str]:
    """Reference the component from each named deployment; returns the ones that changed."""
    targets = set(deployment_names)
    updated: list[str] = []
    # Driven by the deployments, not by the names, so the order of the result (and of the
    # commit message built from it) stays file order.
    for deployment in existing_deployments:
        name = deployment.get("name")
        if not isinstance(name, str) or name not in targets:
            continue
        references = {c.get("reference") for c in deployment.get("components", [])}
        if attachment.name in references:
            continue
        deployment.setdefault("components", []).append({"reference": attachment.name, "image": attachment.image})
        if attachment.root:
            set_domain_setting(deployment, DomainSetting.ROOT_COMPONENT, attachment.name)
        updated.append(name)
    return updated


def _update_description(component: dict[str, Any], description: str | None) -> None:
    """An empty string removes the field; None leaves the current value unchanged."""
    if description is None:
        return
    if description:
        component["description"] = description
    else:
        component.pop("description", None)


def _update_image(component: dict[str, Any], image: str | None) -> str | None:
    """Point the component at ``image``; returns a warning when it had to be lowercased."""
    if image is None:
        return None
    normalized, was_normalized = normalize_container_image(image)
    component["image"] = normalized
    if not was_normalized:
        return None
    return f"Image was normalized to lowercase: '{image}' -> '{normalized}'"


def _update_ports(component: dict[str, Any], port: int | None, ports: list[int] | None) -> None:
    """``ports`` replaces the inbound list wholesale, ``port`` only sets the first one.

    An empty ``ports`` therefore clears the list, with no default fallback, while ``port``
    keeps any extra ports the component has. The two are mutually exclusive, enforced by
    the request model.
    """
    if ports is not None:
        port_block = component.setdefault("ports", {})
        port_block["inbound"] = ports
        port_block.setdefault("outbound", [80, 443])
    elif port is not None:
        port_block = component.setdefault("ports", {})
        inbound = list(port_block.get("inbound") or [])
        if inbound:
            inbound[0] = port
        else:
            inbound = [port]
        port_block["inbound"] = inbound
        port_block.setdefault("outbound", [80, 443])


def _update_path(component: dict[str, Any], path: str | None, rewrite: str | None) -> None:
    """Write match and rewrite into the path entry that is already there."""
    if path is None and not rewrite:
        return

    existing = component.get("path")
    if isinstance(existing, list) and existing and isinstance(existing[0], dict):
        entry = dict(existing[0])
    elif isinstance(existing, str):
        entry = {"match": existing}
    else:
        entry = {}

    if path is not None:
        entry["match"] = path
    if rewrite:
        entry["rewrite"] = rewrite
    entry.setdefault("match", "/")
    component["path"] = [entry]


@dataclass(frozen=True)
class _ServiceChanges:
    """What an update asks of a component's service list.

    ``replace`` wins over the other two, and ``remove`` is applied before ``add``, which is
    what lets one call swap a service out for another.
    """

    replace: list[str] | None = None
    add: list[str] | None = None
    remove: list[str] | None = None


def _selection_error(project_data: dict[str, Any], names: list[str]) -> dict[str, Any] | None:
    """Select these services on the project, or say which one needs a decision first.

    As in add_component: a service that may enrol itself is added at project level here
    (RC-84), one that may not is refused by name.
    """
    try:
        ServiceAdapter.ensure_project_selection(project_data, *names)
    except ServiceValidationError as e:
        return {"success": False, "error": str(e), "error_type": "invalid_services"}
    return None


def _append_new_services(
    project_data: dict[str, Any], component: dict[str, Any], add: list[str]
) -> dict[str, Any] | None:
    """Add the services the component does not have yet, project selection included."""
    error = _selection_error(project_data, add)
    if error:
        return error
    existing = component.get("services") or []
    known = set(ServiceAdapter.extract_service_names_from_project_services(existing))
    component["services"] = existing + ServiceAdapter.build_component_service_entries(
        [service for service in add if service not in known]
    )
    return None


def _update_services(
    project_data: dict[str, Any], component: dict[str, Any], changes: _ServiceChanges
) -> dict[str, Any] | None:
    """Apply the requested service changes, or return why they were refused."""
    if changes.replace is not None:
        error = _selection_error(project_data, changes.replace)
        if error:
            return error
        component["services"] = ServiceAdapter.merge_component_service_entries(
            component.get("services") or [], changes.replace
        )
        return None

    if changes.remove:
        # Component-local only: the entry leaves with its config; the project-level
        # selection stays, because another component may still use the service.
        try:
            ServiceAdapter.parse_services_from_strings(changes.remove)
        except ServiceValidationError as e:
            return {"success": False, "error": str(e), "error_type": "invalid_services"}
        removed = set(changes.remove)
        component["services"] = [
            entry for entry in component.get("services") or [] if service_entry_name(entry) not in removed
        ]
        if changes.add:
            return _append_new_services(project_data, component, changes.add)
        return None

    if changes.add:
        return _append_new_services(project_data, component, changes.add)
    return None


def _find_by_name(entries: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The first entry of this name, or None."""
    return next((entry for entry in entries if entry.get("name") == name), None)


def _normalized_image_warning(image: str) -> tuple[str, str | None]:
    """The image as the OCI spec requires it (lowercase), plus a warning when that changed it."""
    normalized, was_normalized = normalize_container_image(image)
    if not was_normalized:
        return normalized, None
    return normalized, f"Image was normalized to lowercase: '{image}' -> '{normalized}'"


def _refuse_attachment(
    target_deployment: dict[str, Any],
    project_data: dict[str, Any],
    component_name: str,
    deployment_name: str,
    project_name: str,
) -> dict[str, Any] | None:
    """Why this component cannot join this deployment, or None when it can.

    The deployment is found by the caller, which is also where an absent one is refused.
    """
    if _find_by_name(project_data.get("components", []), component_name) is None:
        return {
            "success": False,
            "error": f"Component '{component_name}' not found in project '{project_name}'",
            "error_type": "component_not_found",
        }

    references = {c.get("reference") for c in target_deployment.get("components", [])}
    if component_name in references:
        return {
            "success": False,
            "error": f"Component '{component_name}' is already in deployment '{deployment_name}'",
            "error_type": "duplicate_component_in_deployment",
        }

    return None


class ComponentMutations:
    """The component writers of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ProjectFileAccess) -> None:
        self._host = host

    async def add_component(
        self,
        name: str,
        image: str | None,
        deployment_names: list[str],
        component_type: str = "single",
        description: str | None = None,
        port: int | None = None,
        ports: list[int] | None = None,
        path: str = "/",
        rewrite: str | None = None,
        services: list[str] | None = None,
        cpu_limit: str | None = None,
        memory_limit: str | None = None,
        env_vars: str | None = None,
        aliases: str | None = None,
        root: bool = False,
    ) -> dict[str, Any]:
        """Add a component definition and reference it in the named deployments.

        What the signature does not say:

        * every deployment in ``deployment_names`` must already exist, and the name must be
          K8s-compliant and unused in this project;
        * ``port`` may be None, for a background worker that serves no HTTP;
        * ``path`` and ``rewrite`` only mean something when publish-on-web is selected, and
          ``rewrite`` None leaves the path as the container sees it;
        * ``env_vars`` arrives as ``KEY=value`` lines and is AGE-encrypted before it is
          stored; ``aliases`` is a YAML mapping (``DATABASE_URL: $HOST:$PORT/$DB_NAME``);
        * an empty ``description`` leaves the field out rather than writing it empty;
        * ``root`` marks the component the deployment's bare web address is built around.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            existing_deployments = project_data.get("deployments", [])
            refusal = _refuse_new_component(project_data, name, project_name, deployment_names, root=root)
            if refusal:
                return refusal

            # A service the project has not selected is added here when the service allows
            # it (RC-84), and refused with a message naming it when it needs a project-level
            # decision first.
            if services:
                try:
                    ServiceAdapter.ensure_project_selection(project_data, *services)
                except ServiceValidationError as e:
                    return {"success": False, "error": str(e), "error_type": "invalid_services"}

            # The image lives on the deployment reference and nowhere else, so a component
            # that is not attached anywhere needs no image.
            warnings: list[str] = []
            normalized_image = ""
            if image:
                normalized_image, warning = _normalized_image_warning(image)
                if warning:
                    warnings.append(warning)

            public_key = get_project_public_key(project_data)
            try:
                component_config = await build_component_config(
                    name=name,
                    description=description,
                    component_type=component_type,
                    port=port,
                    ports=ports,
                    path=path,
                    rewrite=rewrite,
                    services=services or [],
                    cpu_limit=cpu_limit,
                    memory_limit=memory_limit,
                    env_vars=env_vars,
                    aliases=aliases,
                    public_key=public_key,
                )
            except (ComponentValidationError, ValueError) as e:
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": "validation_error",
                }

            project_data.setdefault("components", []).append(component_config)

            deployments_updated = _attach_component_to_deployments(
                existing_deployments,
                deployment_names,
                _ComponentAttachment(name=name, image=normalized_image, root=root),
            )

            # Binding a service to the new component can ask for something an administrator
            # has to grant, the same way configuring that service does.
            ensure_approval_requests(project_data)

            await self._host.save_and_commit_project(
                project_data, _add_commit_message(name, project_name, deployments_updated)
            )

            logger.info(f"Successfully added component '{name}' to project '{project_name}'")
            result: dict[str, Any] = {
                "success": True,
                "component": component_config,
                "deployments_updated": deployments_updated,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        except (ProjectSchemaError, ProjectIntegrityError) as e:
            # Expected user-input failure, surfaced to the caller. At WARNING and not
            # ERROR, so a bad component request does not page ops.
            logger.warning("Component '%s' rejected by project validation: %s", name, e)
            return {"success": False, "error": str(e), "error_type": "validation_error"}
        except Exception as e:
            error_msg = f"Error adding component '{name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def delete_component(self, component_name: str, *, confirm_in_use: bool = False) -> dict[str, Any]:
        """Remove a component definition from the project.

        Refuses a component that is still referenced, with ``used_by`` naming every place.
        ``confirm_in_use`` drops every deployment entry and every ``uses-components``
        mention together with the definition, in a single save: a reference pointing at a
        component that no longer exists makes the project file invalid.

        Refused even with the confirmation: a component a deployment's web address is built
        around (``root-component`` or ``expose-component-on-bare-domain``). Removing that
        reference means deciding how the site should be served instead.

        The refusal rule itself lives in ``remove_component``.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            components = project_data.get("components", []) or []
            if not any(isinstance(c, dict) and c.get("name") == component_name for c in components):
                return {
                    "success": False,
                    "error": f"Component '{component_name}' niet gevonden",
                    "error_type": "not_found",
                }

            try:
                uncoupled = remove_component(project_data, component_name, confirm_in_use=confirm_in_use)
            except ComponentInUseError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": "in_use",
                    "used_by": [site.as_dict() for site in e.sites],
                }

            where = f" and removed from {', '.join(site.label for site in uncoupled)}" if uncoupled else ""
            await self._host.save_and_commit_project(
                project_data, f"Remove component '{component_name}' from project '{project_name}'{where}"
            )

            logger.info(f"Successfully removed component '{component_name}' from project '{project_name}'{where}")
            return {"success": True, "uncoupled_from": [site.as_dict() for site in uncoupled]}

        except (ProjectSchemaError, ProjectIntegrityError) as e:
            # The save is the check that a cleanup left no reference behind: a dangling
            # reference is exactly what validate_component_references rejects here.
            logger.warning("Component '%s' removal rejected by project validation: %s", component_name, e)
            return {"success": False, "error": str(e), "error_type": "validation_error"}
        except Exception as e:
            error_msg = f"Error removing component '{component_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def update_component(
        self,
        name: str,
        description: str | None = None,
        image: str | None = None,
        port: int | None = None,
        ports: list[int] | None = None,
        path: str | None = None,
        rewrite: str | None = None,
        services: list[str] | None = None,
        add_services: list[str] | None = None,
        remove_services: list[str] | None = None,
        cpu_limit: str | None = None,
        memory_limit: str | None = None,
    ) -> dict[str, Any]:
        """Update fields of an existing component (partial update).

        Only the provided (non-None) fields are changed; the rest are left as-is.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            components = project_data.get("components", []) or []
            component = next((c for c in components if isinstance(c, dict) and c.get("name") == name), None)
            if component is None:
                return {
                    "success": False,
                    "error": f"Component '{name}' niet gevonden",
                    "error_type": "not_found",
                }

            warnings: list[str] = []

            _update_description(component, description)
            image_warning = _update_image(component, image)
            if image_warning:
                warnings.append(image_warning)
            _update_ports(component, port, ports)
            _update_path(component, path, rewrite)

            service_error = _update_services(
                project_data,
                component,
                _ServiceChanges(replace=services, add=add_services, remove=remove_services),
            )
            if service_error:
                return service_error

            if cpu_limit is not None or memory_limit is not None:
                # A limit set here beats any override the tuner wrote for this component,
                # and is remembered as the user's own value (RC-141). The API knows limits
                # only, never requests.
                self._host._project_file_handler.apply_user_resource_intent(
                    project_data,
                    name,
                    {"limits_cpu": cpu_limit, "limits_memory": memory_limit},
                    origin="api",
                )

            ensure_approval_requests(project_data)

            await self._host.save_and_commit_project(
                project_data, f"Update component '{name}' in project '{project_name}'"
            )

            logger.info(f"Successfully updated component '{name}' in project '{project_name}'")
            result: dict[str, Any] = {"success": True, "component": component}
            if warnings:
                result["warnings"] = warnings
            return result

        except (ProjectSchemaError, ProjectIntegrityError) as e:
            logger.warning("Component '%s' update rejected by project validation: %s", name, e)
            return {"success": False, "error": str(e), "error_type": "validation_error"}
        except Exception as e:
            error_msg = f"Error updating component '{name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def add_component_to_deployment(
        self,
        deployment_name: str,
        component_name: str,
        image: str,
    ) -> dict[str, Any]:
        """Add an existing component to a deployment that does not yet include it.

        Both the deployment and the component definition must already exist, and the
        component must not already be in that deployment: each of the three is a refusal
        with its own ``error_type`` rather than a silent no-op.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            target_deployment = _find_by_name(project_data.get("deployments", []), deployment_name)
            if target_deployment is None:
                return {
                    "success": False,
                    "error": f"Deployment '{deployment_name}' not found in project '{project_name}'",
                    "error_type": "deployment_not_found",
                }

            refusal = _refuse_attachment(target_deployment, project_data, component_name, deployment_name, project_name)
            if refusal:
                return refusal

            warnings: list[str] = []
            normalized_image, warning = _normalized_image_warning(image)
            if warning:
                warnings.append(warning)

            component_ref: dict[str, Any] = {"reference": component_name, "image": normalized_image}
            target_deployment.setdefault("components", []).append(component_ref)

            commit_message = (
                f"Add component '{component_name}' to deployment '{deployment_name}' in project '{project_name}'"
            )
            await self._host.save_and_commit_project(project_data, commit_message)

            logger.info(
                f"Successfully added component '{component_name}' to deployment '{deployment_name}' "
                f"in project '{project_name}'"
            )
            result: dict[str, Any] = {
                "success": True,
                "component_reference": component_ref,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        except Exception as e:
            error_msg = f"Error adding component '{component_name}' to deployment '{deployment_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}
