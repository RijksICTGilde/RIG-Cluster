"""Writing a service's config into the project file, at each of the four layers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.manager.project.notices import approval_notices, config_advice_warnings
from opi.services import ServiceAdapter, ServiceValidationError
from opi.services.approvals import ensure_approval_requests
from opi.services.catalog.base import ConfigLayer
from opi.services.component_values import (
    ComponentValuesError,
    ValuesOperation,
    apply_operation,
    encode,
    locate,
    validate_value_for_storage,
)
from opi.services.component_values import decode as decode_component_values
from opi.services.registry import generate_missing_values, get_service
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.manager.project.access import ProjectFileAccess

logger = logging.getLogger(__name__)


class ServiceConfigMutations:
    """The service-config writers of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ProjectFileAccess) -> None:
        self._host = host

    @staticmethod
    def _add_service_commit_message(project_name: str, service_name: str, result: dict[str, Any]) -> str:
        """Name in the commit what actually changed, selection and binding apart."""
        parts: list[str] = []
        if result["services_added"]:
            parts.append(f"add service(s) {', '.join(result['services_added'])}")
        if result["components_updated"]:
            parts.append(f"bind '{service_name}' to component(s) {', '.join(result['components_updated'])}")
        return f"{'; '.join(parts).capitalize()} in project '{project_name}'"

    async def add_service(
        self,
        service_name: str,
        component_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Select a service on the project, and optionally bind it to components.

        Auto-resolved dependencies of the service are selected along with it. Naming
        components also updates their own ``services`` lists, so select and bind is one
        request.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            try:
                result = ServiceAdapter.add_services_to_project(project_data, [service_name], component_names)
            except ServiceValidationError as e:
                error_type = "invalid_components" if "Components not found" in str(e) else "invalid_service"
                return {"success": False, "error": str(e), "error_type": error_type}

            # Binding an already-selected service to a component is a change too, so the
            # gate cannot be on services_added alone.
            if result["services_added"] or result["components_updated"]:
                # Switching on a service that needs approval has to make the request, as
                # the portal does.
                ensure_approval_requests(project_data)
                commit_message = self._add_service_commit_message(project_name, service_name, result)
                await self._host.save_and_commit_project(project_data, commit_message)

            logger.info(
                f"Add service '{service_name}' to project '{project_name}': "
                f"added={result['services_added']}, skipped={result['services_skipped']}, "
                f"components={result['components_updated']}"
            )

            return {"success": True, **result}

        except Exception as e:
            error_msg = f"Error adding service '{service_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def configure_service(
        self,
        service_name: str,
        target: str,
        config: dict[str, Any] | list[Any],
        *,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one service's config block at a target layer, then persist.

        ``target`` is a ``ConfigLayer`` value (``project`` / ``component`` /
        ``deployment`` / ``deployment-component``). A config the service's model rejects
        surfaces as ``validation_error`` carrying the chokepoint's message, which lists
        the accepted fields.
        """
        try:
            layer = ConfigLayer(target)
        except ValueError:
            return {"success": False, "error": f"Unknown target '{target}'", "error_type": "invalid_target"}

        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            try:
                ServiceAdapter.set_service_config(
                    project_data,
                    service_name,
                    layer,
                    config,
                    component_name=component_name,
                    deployment_name=deployment_name,
                )
            except ServiceValidationError as e:
                return {"success": False, "error": str(e), "error_type": "invalid_target"}

            # What the writer left empty and the service fills in itself, today an invite
            # key.
            generated = generate_missing_values(project_data)

            # A domain or subdomain in this config is on request: without the request no
            # ingress ever appears on the address that was asked for.
            ensure_approval_requests(project_data)

            commit_message = f"Configure service '{service_name}' at {target} target in project '{project_name}'"
            try:
                await self._host.save_and_commit_project(project_data, commit_message)
            except (ProjectSchemaError, ProjectIntegrityError) as e:
                return {"success": False, "error": str(e), "error_type": "validation_error"}

            logger.info(f"Configured service '{service_name}' at {target} target in project '{project_name}'")
            return {
                "success": True,
                "service": service_name,
                "target": target,
                "generated": generated,
                "approvals": approval_notices(project_data, deployment_name),
                "warnings": config_advice_warnings(project_data),
            }

        except Exception as e:
            error_msg = f"Error configuring service '{service_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def clear_service_config(
        self,
        service_name: str,
        target: str,
        *,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, Any]:
        """Remove one service's config at a target layer (demote to bare string).

        Idempotent: removing config that is not there is a success no-op and writes
        nothing to the log. Only a real change commits and logs one INFO line.
        """
        try:
            layer = ConfigLayer(target)
        except ValueError:
            return {"success": False, "error": f"Unknown target '{target}'", "error_type": "invalid_target"}

        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            try:
                removed = ServiceAdapter.remove_service_config(
                    project_data, service_name, layer, component_name=component_name, deployment_name=deployment_name
                )
            except ServiceValidationError as e:
                return {"success": False, "error": str(e), "error_type": "invalid_target"}

            if not removed:
                return {"success": True, "service": service_name, "target": target, "removed": False}

            commit_message = f"Clear service '{service_name}' config at {target} target in project '{project_name}'"
            try:
                await self._host.save_and_commit_project(project_data, commit_message)
            except (ProjectSchemaError, ProjectIntegrityError) as e:
                return {"success": False, "error": str(e), "error_type": "validation_error"}

            logger.info(f"Cleared service '{service_name}' config at {target} target in project '{project_name}'")
            return {"success": True, "service": service_name, "target": target, "removed": True}

        except Exception as e:
            error_msg = f"Error clearing service '{service_name}' config: {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def patch_service_config_list(
        self,
        service_name: str,
        target: str,
        *,
        add: list[Any],
        remove: list[str],
        list_field: str | None = None,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, Any]:
        """Add/update/remove items in one list of a service's config, then persist.

        Only the named items change, on the freshly read project file, so a caller that
        adjusts one storage mount, attachment coupling, invite, access rule or sleep-mode
        pattern never rewrites the rest. ``list_field`` names the list inside an
        object-shaped config and is ``None`` when the config itself is the list. A no-op
        pushes nothing, and the counts in the result say what changed.
        """
        try:
            layer = ConfigLayer(target)
        except ValueError:
            return {"success": False, "error": f"Unknown target '{target}'", "error_type": "invalid_target"}

        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            try:
                counts = ServiceAdapter.patch_service_config_list(
                    project_data,
                    service_name,
                    layer,
                    add=add,
                    remove=remove,
                    list_field=list_field,
                    component_name=component_name,
                    deployment_name=deployment_name,
                )
            except ServiceValidationError as e:
                return {"success": False, "error": str(e), "error_type": "invalid_target"}

            # An added entry can carry an empty invite key just as a whole config can;
            # see configure_service.
            generated = generate_missing_values(project_data)

            commit_message = f"Patch service '{service_name}' config at {target} target in project '{project_name}'"
            try:
                await self._host.save_and_commit_project(project_data, commit_message)
            except (ProjectSchemaError, ProjectIntegrityError) as e:
                return {"success": False, "error": str(e), "error_type": "validation_error"}

            logger.info(f"Patched service '{service_name}' config at {target} target in project '{project_name}'")
            return {
                "success": True,
                "service": service_name,
                "target": target,
                "generated": generated,
                "warnings": config_advice_warnings(project_data),
                **counts,
            }

        except Exception as e:
            error_msg = f"Error patching service '{service_name}' config: {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def set_component_values(
        self,
        service_name: str,
        layer: str,
        operation: str,
        *,
        component_name: str,
        deployment_name: str | None = None,
        values: dict[str, str] | None = None,
        keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add, patch, delete or clear the values a service owns on one component (RC-55).

        The sibling of ``configure_service`` for the two services that own a plain
        component property instead of a block in a ``services:`` list. The whole
        decrypt -> mutate -> re-encrypt round trip belongs inside the change function: a
        ciphertext computed before the read drops the entries of a writer who pushed
        first.

        Returns ``changed=False`` when the resulting values equal what is already stored.
        AGE is not deterministic, so only a comparison after decryption means anything
        here: re-encrypting an unchanged set gives a different ciphertext, and so a
        commit per call.
        """
        service = get_service(ServiceType(service_name))
        owned_property = service.owned_property
        if not service.owned_values_map or owned_property is None:  # pragma: no cover - guarded by the API
            return {
                "success": False,
                "error": f"Service '{service_name}' owns no values",
                "error_type": "invalid_target",
            }
        config_layer = ConfigLayer(layer)
        values_operation = ValuesOperation(operation)
        try:
            for key, value in (values or {}).items():
                # The API refuses these too, with a 422. Repeated here because this is the
                # write path: a value the storage form normalises would differ from what is
                # stored on every read, so the no-op check below could never be true.
                validate_value_for_storage(key, value)
                # And the service's own rule on its values: an alias must point at a
                # platform variable that exists (RC-66, bevinding 5).
                service.validate_owned_value(key, value)
        except ComponentValuesError as e:
            return {"success": False, "error": str(e), "error_type": "invalid_values"}

        def mutator(project_data: dict[str, Any]) -> dict[str, Any] | None:
            node = locate(project_data, config_layer, component_name, deployment_name)
            if node is None:
                raise ComponentValuesError(
                    f"Component '{component_name}' bestaat niet"
                    + (f" in deployment '{deployment_name}'" if deployment_name else "")
                    + "."
                )
            current = decode_component_values(node.get(owned_property), project_data)
            updated = apply_operation(current, values_operation, values=values, keys=keys)
            if updated == current:
                return None
            encoded = encode(updated, project_data)
            if encoded is None:
                node.pop(owned_property, None)
            else:
                node[owned_property] = encoded
            return project_data

        where = f"component '{component_name}'" + (f" in deployment '{deployment_name}'" if deployment_name else "")
        commit_message = f"{operation.capitalize()} {service_name} values on {where}"
        try:
            committed = await self._host.mutate_and_commit_project(mutator, commit_message)
        except ComponentValuesError as e:
            return {"success": False, "error": str(e), "error_type": "invalid_values"}
        except (ProjectSchemaError, ProjectIntegrityError) as e:
            return {"success": False, "error": str(e), "error_type": "validation_error"}

        if committed:
            # The names are not secret, the values are.
            logger.info("Applied %s of %s values on %s", operation, service_name, where)
        return {"success": True, "service": service_name, "target": layer, "changed": committed}
