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
    age_password = """-----BEGIN AGE ENCRYPTED FILE-----
YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSA0K28zZERxZ29ZMjVuQVNP
WEpMU0wwMXNPN2F1T3ZSK2M5TmN4b3RNY3dnCmhLN3FxLzcvN01OdmIxWXVFL00z
dCt0L0drcVFZUTBKOERIZ3NQK3VFUGcKLS0tIFNCQUM3Z1U0MGJ3eTYxeC9Tb29Z
ZmxTWm9BRGtpUExUVlN3N1JPUjRhV0kKt96lbcSOqLThEgvr67Pk3i4IBV6j8mPo
ATTaHv3CMKcMQOrDcJ4Z2ilL6CgB/RUw+5G3mBZ/A0f1n5HdqYfXfLi8slY7348S
DQ==
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

    # Build deployments list
    deployments_list = []
    if project_data.components:
        for idx, comp in enumerate(project_data.components):
            deployments_list.append(
                {
                    "name": f"deployment-{idx + 1}",
                    "cluster": project_data.cluster,
                    "namespace": project_data.project_name,
                    "repository": "main-repo",
                    "components": [{"reference": f"component-{idx + 1}", "image": comp.image or "nginx:latest"}],
                }
            )
    else:
        # Default deployment
        deployments_list.append(
            {
                "name": "main",
                "cluster": project_data.cluster,
                "namespace": project_data.project_name,
                "repository": "main-repo",
                "components": [{"reference": "main", "image": "nginx:latest"}],
            }
        )

    # Create project structure
    project_config = {
        "name": project_data.project_name,
        "display-name": project_data.display_name,
        "description": project_data.project_description or "Project created via self-service portal",
        "clusters": [project_data.cluster],
        "services": [service.value for service in project_services],  # Project-level services
        "config": {
            "age-public-key": public_key,
            "age-private-key": LiteralScalarString(encrypted_private_key),
            "api-key": LiteralScalarString(encrypted_api_key),
        },
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
