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

    # Default encrypted password for git repository
    # TODO: this should be read from the config file
    age_password = """-----BEGIN AGE ENCRYPTED FILE-----
YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBwYUZMYTVuRGxvbkR6MzVQ
K1lUUTNjZnp6YXF0TUplZ2VsenJ4QkM1empvCi9GMjJPVWRMckRtRjE2Y3NmYWpU
dFN0c3dyUlo0cFdLT0ZxaXVxSmpVcU0KLS0tIGQxRmRWVHdaMWUwWjhtRUovZysy
dU9MZjR0VVJ1SXVHT1YwcHdVUzBpcTgK3oaTxov0EmQqY+F9SZH3V0N4qWwnDHIe
28SNnwfqikaAa5tcVrb/9n13pK7sDAT6mzYKsJxXqt5tRzIylTXy9vI4DKbrdbJi
-----END AGE ENCRYPTED FILE-----
"""

    # Parse project-level services using the service adapter
    project_services = ServiceAdapter.parse_services_from_strings(project_data.services or [])

    # Build components list from form data
    components_list = []
    if project_data.components:
        for idx, comp in enumerate(project_data.components):
            # Parse component-level services
            component_services = ServiceAdapter.parse_services_from_strings(comp.services or [])

            component_config = {
                "name": f"component-{idx + 1}",
                "type": comp.type,
                "ports": {"inbound": [comp.port] if comp.port else [8080], "outbound": [80, 443]},
                "path": comp.path,  # Publication path for ingress routing
                "uses-services": [service.value for service in component_services],
                "uses-components": [],
            }

            # Add storage configurations from services
            storage_configs = ServiceAdapter.create_storage_configs(component_services)
            if storage_configs:
                component_config["storage"] = storage_configs

            # Add resource limits if specified
            if comp.cpu_limit or comp.memory_limit:
                component_config["resources"] = {}
                if comp.cpu_limit:
                    component_config["resources"]["cpu"] = comp.cpu_limit
                if comp.memory_limit:
                    component_config["resources"]["memory"] = comp.memory_limit

            try:
                if comp.env_vars:
                    encrypted_env_vars = await encrypt_age_content(comp.env_vars, public_key)
                    component_config["user-env-vars"] = LiteralScalarString(encrypted_env_vars)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            # Add aliases if provided (no encryption needed - they reference system variables)
            if comp.aliases:
                try:
                    # Parse aliases as YAML to validate format
                    yaml_instance = YAML()
                    aliases_dict = yaml_instance.load(comp.aliases)
                    if aliases_dict and isinstance(aliases_dict, dict):
                        component_config["aliases"] = aliases_dict
                except Exception as e:
                    logger.warning(f"Failed to parse aliases for component {idx + 1}: {e}")
                    raise HTTPException(status_code=400, detail=f"Invalid aliases format: {e!s}")

            components_list.append(component_config)
    else:
        # Default component if none specified
        # Create fallback component with project-level services
        fallback_component_config = {
            "name": "main",
            "type": "deployment",
            "ports": {"inbound": [8080], "outbound": [80, 443]},
            "uses-services": [service.value for service in project_services],
            "uses-components": [],
        }

        # Add storage configurations from project services
        storage_configs = ServiceAdapter.create_storage_configs(project_services)
        if storage_configs:
            fallback_component_config["storage"] = storage_configs

        components_list.append(fallback_component_config)

    # Build deployments list - create ONE deployment with all components
    deployments_list = []
    if project_data.components:
        # Build component references for all components
        component_refs = []
        for idx, comp in enumerate(project_data.components):
            component_refs.append({"reference": f"component-{idx + 1}", "image": comp.image or "nginx:latest"})

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
            if hasattr(project_data, "include_project_name") and project_data.include_project_name:
                deployment_config["include-project-name"] = True
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
            if hasattr(project_data, "include_project_name") and project_data.include_project_name:
                deployment_config["include-project-name"] = True
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
    project_config = {
        "name": project_data.project_name,
        "display-name": project_data.display_name,
        "description": project_data.project_description or "Project created via self-service portal",
        "clusters": [project_data.cluster],
        "services": [service.value for service in project_services],  # Project-level services
        "config": config_section,
        "repositories": [
            {
                "name": "main-repo",
                "url": "https://github.com/RijksICTGilde/rig-cluster-application-test.git",
                "username": "git",
                "password": age_password,
                "branch": "main",
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
