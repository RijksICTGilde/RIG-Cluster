"""Reading, categorising and resolving the aliases and env vars of a deployment."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from opi.services import ServiceAdapter, ServiceType
from opi.services.catalog.aliases.references import variable_to_service
from opi.services.component_values import ComponentValuesError
from opi.services.component_values import decode as decode_component_values
from opi.services.project import Project
from opi.utils.env_vars import (
    detect_circular_references,
    extract_variable_references,
    substitute_variables,
)
from opi.utils.naming import generate_public_url
from opi.utils.yaml_util import compiled_jsonpath

logger = logging.getLogger(__name__)


def get_service_category_name(service_type: ServiceType) -> str:
    """The category name of a service, as alias resolution uses it.

    Both the shared and the namespace-dedicated variant of postgresql and redis collapse
    onto one category, so an alias on ``$REDIS_URL`` routes to the same secret bucket
    whichever variant the project picked. Without that collapse the variable-to-service
    map ends up keyed by the namespace variant (later overwrites earlier in dict
    iteration), the alias categorises as "namespace-redis", a bucket no project consumes,
    and it silently drops out.
    """
    category_map = {
        ServiceType.POSTGRESQL_DATABASE: "database",
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE: "database",
        ServiceType.REDIS: "redis",
        ServiceType.NAMESPACE_REDIS: "redis",
        ServiceType.MINIO_STORAGE: "minio",
        # seaweedfs-storage reuses MinIOVariables, so the same collapse: otherwise an
        # OBJECT_STORE_* alias routes to a bucket no project consumes.
        ServiceType.SEAWEEDFS_STORAGE: "minio",
        ServiceType.KEYCLOAK: "keycloak",
        ServiceType.PUBLISH_ON_WEB: "web",
        ServiceType.PERSISTENT_STORAGE: "storage",
        ServiceType.TEMP_STORAGE: "storage",
        ServiceType.PLATFORM: "platform",
    }
    return category_map.get(service_type, service_type.value)


def categorize_alias(alias_name: str, alias_template: str) -> tuple[str, str]:
    """Which service and source type an alias belongs to, from the variables it names.

    Returns the category of the one service whose variables the template references, and
    either ``"secret"`` or ``"direct"``.

    Raises:
        ValueError: If the alias references no variable at all, an unknown variable,
            variables of more than one service, or a mix of secret and direct variables.
    """
    referenced_vars = extract_variable_references(alias_template)

    if not referenced_vars:
        raise ValueError(
            f"Alias '{alias_name}' has no variable references. Aliases must reference at least one service variable."
        )

    var_to_service = variable_to_service()
    all_known_vars = set(var_to_service)

    unknown_vars = [var for var in referenced_vars if var not in all_known_vars]
    if unknown_vars:
        known_vars_list = ", ".join(sorted(all_known_vars))
        raise ValueError(
            f"Alias '{alias_name}' references unknown variables: {', '.join(unknown_vars)}. "
            f"Available variables: {known_vars_list}"
        )

    services_referenced: dict[str, ServiceType] = {}
    source_types: set[str] = set()

    for var in referenced_vars:
        service_type, var_def = var_to_service[var]
        service_category = get_service_category_name(service_type)
        services_referenced[service_category] = service_type
        source_types.add(var_def.source)

    if len(services_referenced) > 1:
        raise ValueError(
            f"Alias '{alias_name}' references variables from multiple services: {', '.join(services_referenced.keys())}. "
            f"Each alias must reference variables from only one service."
        )

    if len(source_types) > 1:
        raise ValueError(
            f"Alias '{alias_name}' mixes variables from different sources: {', '.join(source_types)}. "
            f"Each alias must use variables from only one source type (either 'secret' or 'direct')."
        )

    service_category = next(iter(services_referenced.keys()))
    source_type = next(iter(source_types))

    logger.debug(f"Alias '{alias_name}' categorized as service='{service_category}', source='{source_type}'")
    return service_category, source_type


def scope_direct_aliases_to_component(
    direct_aliases: dict[str, dict[str, str]],
    project_data: dict[str, Any],
    component_name: str,
) -> dict[str, dict[str, str]]:
    """Narrow deployment-wide direct aliases to those *component_name* declares.

    Aliases are collected per deployment because the secret-sourced ones merge into a
    single shared secret per service. Direct ones resolve against the component's OWN env
    vars (PUBLIC_HOSTNAME from publish-on-web, DATA_PATH from storage), so handing every
    component the deployment-wide set makes a sibling with neither service resolve
    another component's aliases against an empty context, which raises "Variable
    references not found in context".

    Service categories left without an alias are dropped.
    """
    raw_aliases = next(
        (
            component.get("aliases")
            for component in project_data.get("components", [])
            if component.get("name") == component_name
        ),
        None,
    )
    # Decoded rather than read as a mapping: the stored shape is one AGE block, so
    # membership has to be tested on the decrypted names (RC-106). Without this the
    # test below is a substring/character test on a ciphertext and every direct
    # alias silently drops out.
    own_aliases = decode_component_values(raw_aliases, project_data)
    if not own_aliases:
        return {}

    scoped: dict[str, dict[str, str]] = {}
    for service_category, service_aliases in direct_aliases.items():
        selected = {name: template for name, template in service_aliases.items() if name in own_aliases}
        if selected:
            scoped[service_category] = selected
    return scoped


def resolve_aliases(aliases: dict[str, str], context: dict[str, str]) -> dict[str, str]:
    """Resolve the variable references in *aliases* against *context*.

    Never logs a resolved value: an alias may carry a secret.

    Raises:
        ValueError: On a circular reference, or on a reference the context does not hold.
    """
    if not aliases:
        return {}

    logger.debug(f"Resolving {len(aliases)} aliases with context containing {len(context)} variables")

    try:
        detect_circular_references(aliases)
    except ValueError as e:
        logger.error(f"Circular reference detected in aliases: {e}")
        raise

    resolved: dict[str, str] = {}

    for alias_name, alias_template in aliases.items():
        try:
            # Substitution also validates that every referenced variable exists.
            resolved_value = substitute_variables(alias_template, context)
            resolved[alias_name] = resolved_value

            logger.debug(f"Successfully resolved alias: {alias_name}")

        except ValueError as e:
            logger.error(f"Failed to resolve alias '{alias_name}': {e}")
            raise ValueError(f"Failed to resolve alias '{alias_name}': {e}") from e

    logger.info(f"Successfully resolved {len(resolved)} aliases")
    return resolved


def generate_storage_env_vars_from_services(storage_configs: list[dict[str, Any]]) -> dict[str, str]:
    """The direct env vars the storage services declare, each set to its mount path.

    Raises:
        ValueError: On a storage type other than persistent or ephemeral.
    """
    env_vars = {}

    for storage in storage_configs:
        mount_path = storage.get("mount-path")
        storage_type = storage.get("type", "persistent")

        if not mount_path:
            continue

        if storage_type == "persistent":
            service_type = ServiceType.PERSISTENT_STORAGE
        elif storage_type == "ephemeral":
            service_type = ServiceType.TEMP_STORAGE
        else:
            raise ValueError(f"Unknown storage type: {storage_type}")

        for var_def in ServiceAdapter.get_service_definition(service_type).variables:
            if var_def.source == "direct":
                env_vars[var_def.name] = mount_path
                logger.debug(f"Generated storage env var: {var_def.name}={mount_path}")

    return env_vars


def generate_web_env_vars_from_services(hostname: str, use_https: bool = True) -> dict[str, str]:
    """The direct env vars publish-on-web declares for *hostname*.

    PUBLIC_HOST carries the scheme and PUBLIC_HOSTNAME does not: an application whose
    host-name field rejects a URL needs the bare name.
    """
    env_vars = {}

    for var_def in ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB).variables:
        if var_def.source == "direct" and var_def.name == "PUBLIC_HOST":
            public_url = generate_public_url(hostname, use_https)
            env_vars[var_def.name] = public_url
            logger.debug(f"Generated web env var: {var_def.name}={public_url}")
        elif var_def.source == "direct" and var_def.name == "PUBLIC_HOSTNAME":
            env_vars[var_def.name] = hostname
            logger.debug(f"Generated web env var: {var_def.name}={hostname}")

    return env_vars


class ProjectContentsAccess(Protocol):
    """What the deployment aliases reach from their host: the project file."""

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]: ...


class DeploymentAliases:
    """The alias collection of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ProjectContentsAccess) -> None:
        self._host = host

    async def collect_deployment_aliases(self, deployment_name: str) -> dict[str, dict[str, dict[str, str]]]:
        """Collect the aliases of every component in a deployment, categorised.

        Returns ``source_type -> service_category -> {alias_name: template}``, with
        ``"direct"`` and ``"secret"`` always present as the two source types.

        Raises:
            ValueError: If an alias has invalid references, or if two components declare the
                same alias with a different value: the aliases of one service merge into a
                single shared secret, which cannot hold two values.
        """
        logger.debug(f"Collecting aliases for deployment: {deployment_name}")

        project_data = await self._host.get_contents(record_base=False)

        deployments = project_data.get("deployments", [])
        deployment = Project.locate(deployments, name=deployment_name)

        if not deployment:
            logger.warning(f"Deployment '{deployment_name}' not found in project data")
            return {"direct": {}, "secret": {}}

        categorized_aliases: dict[str, dict[str, dict[str, str]]] = {
            "direct": {},
            "secret": {},
        }

        components = deployment.get("components", [])
        for component in components:
            component_name = component["reference"]
            component_definition = await self._get_by_json_path(f"$.components[?@.name=='{component_name}']")
            if not component_definition:
                logger.warning("Component '%s' referenced in deployment but not found in project", component_name)
                continue
            # Aliases are stored as ONE AGE block whose plaintext is KEY=value lines
            # (RC-106), so reading them is a decrypt-and-parse and never a walk over the
            # stored value. Through the shared decoder, which also still reads the
            # unencrypted mapping and the per-value shape aliases used to be written in
            # -- this is the deploy path, and an alias that fell away silently here is a
            # running pod with a missing variable.
            try:
                component_aliases = decode_component_values(component_definition.get("aliases"), project_data)
            except ComponentValuesError as e:
                raise ValueError(
                    f"Aliases of component '{component_name}' in deployment '{deployment_name}' could not be read: {e}"
                ) from e

            if not component_aliases:
                continue

            logger.debug(f"Found {len(component_aliases)} aliases in component '{component_name}'")

            for alias_name, alias_template in component_aliases.items():
                try:
                    service_category, source_type = categorize_alias(alias_name, alias_template)
                except ValueError as e:
                    raise ValueError(
                        f"Error in component '{component_name}', deployment '{deployment_name}': {e}"
                    ) from e

                if service_category not in categorized_aliases[source_type]:
                    categorized_aliases[source_type][service_category] = {}

                # Aliases are declared per component but merged into one shared secret per
                # service for the deployment, so two components declaring the same alias is
                # fine ONLY if the value matches.
                existing = categorized_aliases[source_type][service_category].get(alias_name)
                if existing is not None and existing != alias_template:
                    # Unresolvable: a single shared secret cannot hold two values.
                    raise ValueError(
                        f"Conflicting alias '{alias_name}' in deployment '{deployment_name}': "
                        f"component '{component_name}' defines a different value than another "
                        f"component. Aliases for a service are merged into one shared secret, "
                        f"so they must match."
                    )
                if existing is not None:
                    logger.debug(
                        f"Alias '{alias_name}' re-declared identically by component "
                        f"'{component_name}' in deployment '{deployment_name}' (expected for shared services)"
                    )

                categorized_aliases[source_type][service_category][alias_name] = alias_template

        total_aliases = 0
        summary_parts = []
        for source_type, service_dict in categorized_aliases.items():
            for service_category, aliases in service_dict.items():
                count = len(aliases)
                if count > 0:
                    total_aliases += count
                    summary_parts.append(f"{source_type}.{service_category}: {count}")

        if total_aliases > 0:
            summary = ", ".join(summary_parts)
            logger.info(f"Collected {total_aliases} aliases for deployment '{deployment_name}' ({summary})")

        return categorized_aliases

    async def _get_by_json_path(self, json_path: str) -> Any:
        """The value at *json_path* in the project file, or None when nothing matches."""
        project_data = await self._host.get_contents(record_base=False)

        try:
            jsonpath_expr = compiled_jsonpath(json_path)
            matches = jsonpath_expr.find(project_data)
            return matches[0].value if matches else None
        except Exception as e:
            raise Exception(f"Error querying JSONPath '{json_path}'") from e
