"""
Project utility functions for validation and YAML generation.

This module contains functions that are used across multiple modules for project operations.
Extracted to avoid circular import issues.
"""

import logging
import re
from io import StringIO
from typing import Any

from fastapi import HTTPException
from opi.core.config import settings
from opi.services import ServiceAdapter
from opi.utils.age import encrypt_age_content
from opi.utils.api_keys import generate_api_key
from opi.utils.sops import generate_sops_key_pair
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

logger = logging.getLogger(__name__)


class ComponentValidationError(ValueError):
    """Raised when component configuration validation fails."""


def validate_component_paths(component_paths: list[str], domain_mode: str) -> None:
    """
    Validate path uniqueness for shared-domain modes.

    When using shared domains (deployment-name, custom), all component paths
    within a deployment must be unique to enable correct routing.

    Args:
        component_paths: List of paths from all components in the deployment
        domain_mode: The deployment's domain mode

    Raises:
        ValueError: If duplicate paths are found in a shared-domain mode
    """
    if domain_mode not in ("deployment-name", "custom"):
        return

    seen_paths: set[str] = set()
    duplicate_paths: list[str] = []
    for path in component_paths:
        if path in seen_paths:
            duplicate_paths.append(path)
        seen_paths.add(path)

    if duplicate_paths:
        raise ComponentValidationError(
            f"When using shared domains (domain mode: {domain_mode}), all component paths must be unique. "
            f"Duplicate paths found: {', '.join(duplicate_paths)}. "
            f"Please assign different paths to each component (e.g., /, /api, /admin)."
        )


def validate_root_component(components_with_root: list[tuple[str, bool, int | None]], domain_mode: str) -> None:
    """
    Validate root component constraints.

    In nice-url mode, at most one component can be the root, and it must have a port.

    Args:
        components_with_root: List of (name, is_root, port) tuples for all components in the deployment
        domain_mode: The deployment's domain mode

    Raises:
        ValueError: If root component constraints are violated
    """
    root_components = [(name, port) for name, is_root, port in components_with_root if is_root]

    if not root_components:
        return

    if domain_mode != "nice-url":
        root_names = [name for name, _ in root_components]
        raise ComponentValidationError(
            f"Root component flag is only valid in nice-url domain mode, "
            f"but domain mode is '{domain_mode}'. Components marked as root: {', '.join(root_names)}"
        )

    if len(root_components) > 1:
        root_names = [name for name, _ in root_components]
        raise ComponentValidationError(
            f"In nice-url mode, only one component can be marked as root. "
            f"Found {len(root_components)} root components: {', '.join(root_names)}"
        )

    root_name, root_port = root_components[0]
    if root_port is None:
        raise ComponentValidationError(
            f"Component '{root_name}' is marked as root but has no port specified. "
            f"Root component must have a port for web publishing."
        )


def parse_aliases(aliases_str: str) -> dict[str, Any]:
    """
    Parse a YAML aliases string into a dictionary.

    Aliases allow components to reference system-provided variables using
    $VARIABLE_NAME syntax (e.g., DATABASE_URL: $HOST:$PORT/$DB_NAME).

    Args:
        aliases_str: YAML-formatted string of alias definitions

    Returns:
        Parsed dictionary of aliases

    Raises:
        ValueError: If the aliases string is not valid YAML or not a dict
    """
    try:
        yaml_instance = YAML()
        aliases_dict = yaml_instance.load(aliases_str)
        if aliases_dict and isinstance(aliases_dict, dict):
            return aliases_dict
        return {}
    except Exception as e:
        raise ComponentValidationError(f"Invalid aliases format: {e!s}") from e


async def build_component_config(
    name: str,
    component_type: str,
    port: int | None,
    path: str,
    services: list[str],
    cpu_limit: str | None = None,
    memory_limit: str | None = None,
    env_vars: str | None = None,
    aliases: str | None = None,
    root: bool = False,
    public_key: str | None = None,
    default_port: int | None = None,
) -> dict[str, Any]:
    """
    Build a complete component config dict.

    This is the shared component building logic used by both project creation
    and the add-component API.

    Args:
        name: Component name
        component_type: Component type (e.g., "single", "frontend", "backend")
        port: Inbound port (None for background workers)
        path: Ingress path (e.g., "/", "/api")
        services: Component's services list as strings
        cpu_limit: CPU limit (e.g., "1", "500m")
        memory_limit: Memory limit (e.g., "256Mi", "1Gi")
        env_vars: Environment variables in KEY=value format (will be encrypted)
        aliases: YAML string of alias definitions
        root: Whether this is the root component (nice-url mode)
        public_key: AGE public key for encrypting env vars
        default_port: Default port if none specified (e.g., 8080 for project creation)

    Returns:
        Component configuration dictionary
    """
    inbound_ports = [port] if port else ([default_port] if default_port else [])
    # Build services list in v2 format (mixed string/dict)
    services_list = ServiceAdapter.build_component_service_entries(services)

    component_config: dict[str, Any] = {
        "name": name,
        "type": component_type,
        "ports": {"inbound": inbound_ports, "outbound": [80, 443]},
        "path": path,
        "services": services_list,
        "uses-components": [],
    }

    # Add root flag for nice-url mode
    if root:
        component_config["root"] = True

    # Add resource limits if specified
    if cpu_limit or memory_limit:
        component_config["resources"] = {}
        if cpu_limit:
            component_config["resources"]["cpu"] = cpu_limit
        if memory_limit:
            component_config["resources"]["memory"] = memory_limit

    # Encrypt and add user env vars if provided
    if env_vars:
        if not public_key:
            logger.warning(f"Could not encrypt env_vars for component '{name}': no AGE public key available")
        else:
            encrypted_env_vars = await encrypt_age_content(env_vars, public_key)
            component_config["user-env-vars"] = LiteralScalarString(encrypted_env_vars)

    # Add aliases if provided (no encryption needed - they reference system variables)
    if aliases:
        aliases_dict = parse_aliases(aliases)
        if aliases_dict:
            component_config["aliases"] = aliases_dict

    return component_config


def normalize_container_image(image: str) -> tuple[str, bool]:
    """
    Normalize a container image reference to lowercase.

    The OCI Distribution Specification requires repository names to be lowercase.
    This function ensures compliance and reports if normalization was needed.

    Args:
        image: Container image reference (e.g., "ghcr.io/Org/Repo:tag")

    Returns:
        Tuple of (normalized_image, was_normalized) where was_normalized is True
        if the original image contained uppercase characters that were lowercased.
    """
    normalized = image.lower()
    was_normalized = normalized != image
    if was_normalized:
        logger.warning(
            f"Container image contained uppercase characters and was normalized: '{image}' -> '{normalized}'"
        )
    return normalized, was_normalized


def validate_project_name(name: str) -> bool:
    """
    Validate project name: must start with lowercase letter, then lowercase a-z, numbers 0-9, dash -, max 20 characters.

    Args:
        name: The project name to validate

    Returns:
        True if valid, False otherwise
    """
    if not name:
        return False
    if len(name) > 20:
        return False
    # Must start with a lowercase letter, then can contain lowercase letters, numbers, and dashes
    return re.match(r"^[a-z][a-z0-9-]*$", name) is not None


# should_encrypt_user_env_var function removed - all user env vars are now always encrypted


async def generate_self_service_project_yaml(project_data: Any) -> str:
    """
    Generate project YAML from self-service form data.

    This creates a comprehensive project configuration with:
    - Multiple components if specified
    - Team member configurations
    - Service integrations
    - Resource limits

    Args:
        project_data: The self-service project request data (SelfServiceProjectRequest)

    Returns:
        YAML string representing the project configuration
    """
    # Generate AGE key pair for this project
    try:
        private_key, public_key = generate_sops_key_pair()
        # Encrypt the private key with the global SOPS AGE key for storage
        encrypted_private_key = await encrypt_age_content(private_key, settings.SOPS_AGE_PUBLIC_KEY)
        logger.debug(f"Generated AGE key pair for project: {project_data.project_name}")
    except Exception as e:
        logger.error(f"Failed to generate AGE key pair: {e}")
        raise HTTPException(status_code=500, detail=f"Cannot create project: AGE key generation failed. {e!s}")

    # Generate and encrypt API key using project's public key
    try:
        plain_api_key = generate_api_key()
        encrypted_api_key = await encrypt_age_content(plain_api_key, public_key)
        logger.debug(f"Successfully generated and encrypted API key for project: {project_data.project_name}")
    except Exception as e:
        logger.error(f"Failed to generate encrypted API key: {e}")
        raise HTTPException(status_code=500, detail=f"Cannot create project: API key encryption failed. {e!s}")

    # Repository password from settings (supports plain:, age:, base64+age: prefixes)
    repo_password = settings.PROJECT_REPO_PASSWORD

    # Parse project-level services using the service adapter
    project_services = ServiceAdapter.parse_services_from_strings(project_data.services or [])

    # Build components list from form data
    components_list = []
    if project_data.components:
        for idx, comp in enumerate(project_data.components):
            try:
                component_config = await build_component_config(
                    name=f"component-{idx + 1}",
                    component_type=comp.type,
                    port=comp.port,
                    path=comp.path,
                    services=comp.services or [],
                    cpu_limit=comp.cpu_limit,
                    memory_limit=comp.memory_limit,
                    env_vars=comp.env_vars,
                    aliases=comp.aliases,
                    root=comp.root,
                    public_key=public_key,
                    default_port=8080,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            components_list.append(component_config)
    else:
        # Default component if none specified
        # Create fallback component with project-level services (v2 format)
        fallback_services_list = ServiceAdapter.build_component_service_entries([svc.value for svc in project_services])

        fallback_component_config: dict[str, Any] = {
            "name": "main",
            "type": "deployment",
            "ports": {"inbound": [8080], "outbound": [80, 443]},
            "services": fallback_services_list,
            "uses-components": [],
        }

        components_list.append(fallback_component_config)

    # Build deployments list - create ONE deployment with all components
    deployments_list = []
    if project_data.components:
        # Build component references for all components
        component_refs = []
        for idx, comp in enumerate(project_data.components):
            image, _ = normalize_container_image(comp.image or "nginx:latest")
            component_refs.append({"reference": f"component-{idx + 1}", "image": image})

        # Create a single deployment with all components
        # Use deployment_name from form data (defaults to "main")
        deployment_name = project_data.deployment_name
        deployment_config = {
            "name": deployment_name,
            "cluster": project_data.cluster,
            "namespace": project_data.project_name,
            "repository": "main-repo",
            "components": component_refs,
        }

        # Add subdomain based on domain-mode
        if project_data.domain_mode == "deployment-name":
            deployment_config["subdomain"] = deployment_name
        elif project_data.domain_mode == "custom" and project_data.subdomain:
            deployment_config["subdomain"] = project_data.subdomain
        elif project_data.domain_mode == "nice-url":
            deployment_config["domain-mode"] = "nice-url"
            # For nice-url mode, subdomain is required and globally unique
            if hasattr(project_data, "subdomain") and project_data.subdomain:
                deployment_config["subdomain"] = project_data.subdomain
        # For "component-specific" mode, don't add subdomain field

        # Add external domain configuration if specified
        if hasattr(project_data, "base_domain") and project_data.base_domain:
            deployment_config["base-domain"] = project_data.base_domain
        if hasattr(project_data, "issuer") and project_data.issuer:
            deployment_config["issuer"] = project_data.issuer

        deployments_list.append(deployment_config)
    else:
        # Default deployment
        # Use deployment_name from form data (defaults to "main")
        deployment_name = project_data.deployment_name
        deployment_config = {
            "name": deployment_name,
            "cluster": project_data.cluster,
            "namespace": project_data.project_name,
            "repository": "main-repo",
            "components": [{"reference": "main", "image": "nginx:latest"}],
        }

        # Add subdomain based on domain-mode
        if project_data.domain_mode == "deployment-name":
            deployment_config["subdomain"] = deployment_name
        elif project_data.domain_mode == "custom" and project_data.subdomain:
            deployment_config["subdomain"] = project_data.subdomain
        elif project_data.domain_mode == "nice-url":
            deployment_config["domain-mode"] = "nice-url"
            # For nice-url mode, subdomain is required and globally unique
            if hasattr(project_data, "subdomain") and project_data.subdomain:
                deployment_config["subdomain"] = project_data.subdomain
        # For "component-specific" mode, don't add subdomain field

        # Add external domain configuration if specified
        if hasattr(project_data, "base_domain") and project_data.base_domain:
            deployment_config["base-domain"] = project_data.base_domain
        if hasattr(project_data, "issuer") and project_data.issuer:
            deployment_config["issuer"] = project_data.issuer

        deployments_list.append(deployment_config)

    # Build config section
    config_section = {
        "age-public-key": public_key,
        "age-private-key": LiteralScalarString(encrypted_private_key),
        "api-key": LiteralScalarString(encrypted_api_key),
    }

    # Add contact-email if specified (overrides cluster default for Let's Encrypt)
    if hasattr(project_data, "contact_email") and project_data.contact_email:
        config_section["contact-email"] = project_data.contact_email

    # Create project structure
    from opi.services.schema_migration import LATEST_SCHEMA_VERSION

    project_config = {
        "schema-version": LATEST_SCHEMA_VERSION,
        "name": project_data.project_name,
        "display-name": project_data.display_name,
        "description": project_data.project_description or "Project created via self-service portal",
        "clusters": [project_data.cluster],
        "services": [service.value for service in project_services],  # Project-level services
        "config": config_section,
        "repositories": [
            {
                "name": "main-repo",
                "url": settings.PROJECT_REPO_URL,
                "username": settings.PROJECT_REPO_USERNAME,
                "password": repo_password,
                "branch": settings.PROJECT_REPO_BRANCH,
                "path": ".",
            }
        ],
        "components": components_list,
        "deployments": deployments_list,
    }

    # Add users if provided
    if project_data.user_email and project_data.user_role:
        users = []
        for email, role in zip(project_data.user_email, project_data.user_role, strict=False):
            if email and email.strip():  # Skip empty entries
                users.append({"email": email.strip(), "role": role})
        if users:
            project_config["users"] = users

    # Use ruamel.yaml for proper multiline string handling
    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096  # Prevent line wrapping

    # Handle multiline password with literal block scalar
    password = project_config["repositories"][0]["password"]
    if password and "\n" in password:
        project_config["repositories"][0]["password"] = LiteralScalarString(password)

    # Handle multiline API key with literal block scalar
    api_key = project_config["config"]["api-key"]
    if api_key and "\n" in api_key:
        project_config["config"]["api-key"] = LiteralScalarString(api_key)

    # Generate YAML content
    yaml_output = StringIO()
    yaml_instance.dump(project_config, yaml_output)
    yaml_content = yaml_output.getvalue()

    logger.info(
        f"Generated project YAML for {project_data.project_name} with {len(components_list)} components "
        f"and {len(deployments_list)} deployments"
    )

    return yaml_content
