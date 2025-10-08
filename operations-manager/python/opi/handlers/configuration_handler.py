# TODO THIS CLASS IS NOT USED AND MAY BE REMOVED OR REVIVED

"""
Configuration Handler for collecting and serializing project configuration data.

This module provides a handler that collects configuration information during
project processing and can serialize it to YAML for overview and reference.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)


@dataclass
class EnvVarReference:
    """Reference to an environment variable from a secret."""

    name: str
    secret_name: str
    key: str | None = None  # Key within the secret, defaults to same as name


@dataclass
class ComponentConfiguration:
    """Configuration data for a single component."""

    name: str
    type: str
    namespace: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    derived_env_vars: dict[str, str] = field(default_factory=dict)
    secret_env_vars: dict[str, EnvVarReference] = field(default_factory=dict)  # env_name -> secret reference
    env_from_secrets: list[str] = field(default_factory=list)
    web_addresses: list[str] = field(default_factory=list)
    storage: dict[str, Any] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    argocd_resources: list[str] = field(default_factory=list)
    custom_config: dict[str, Any] = field(default_factory=dict)
    sso_config: dict[str, Any] = field(default_factory=dict)


class ConfigurationHandler:
    """Handler for collecting and managing project configuration data."""

    def __init__(self, project_name: str, project_data: dict[str, Any]):
        """
        Initialize the configuration handler.

        Args:
            project_name: Name of the project
            project_data: Original project configuration data
        """
        self.project_name = project_name
        self.project_data = project_data
        self.components: dict[str, ComponentConfiguration] = {}
        self.global_config: dict[str, Any] = {}

        logger.debug(f"Initialized ConfigurationHandler for project: {project_name}")

    def add_component(self, name: str, component_type: str, namespace: str | None = None) -> None:
        """
        Add a new component to track.

        Args:
            name: Component name
            component_type: Type of component (e.g., 'application', 'database', 'service')
            namespace: Kubernetes namespace for the component
        """
        logger.info(
            f"ConfigHandler.add_component called with name='{name}', type='{component_type}', namespace='{namespace}'"
        )
        if name not in self.components:
            self.components[name] = ComponentConfiguration(name=name, type=component_type, namespace=namespace)
            logger.info(f"ConfigHandler: Successfully added component '{name}' with namespace '{namespace}'")
        else:
            logger.warning(f"ConfigHandler: Component '{name}' already exists, not overwriting")

    def add_env_var(self, component_name: str, key: str, value: str) -> None:
        """
        Add an environment variable for a component.

        Args:
            component_name: Name of the component
            key: Environment variable name
            value: Environment variable value
        """
        logger.info(f"ConfigHandler.add_env_var called with component='{component_name}', key='{key}', value='{value}'")
        if component_name not in self.components:
            logger.error(
                f"ConfigHandler: Component '{component_name}' not found. Available components: {list(self.components.keys())}"
            )
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        self.components[component_name].env_vars[key] = value
        logger.info(f"ConfigHandler: Successfully added env var '{key}' to component '{component_name}'")

    def add_derived_env_var(self, component_name: str, key: str, value: str) -> None:
        """
        Add a derived environment variable for a component (from secrets).

        Args:
            component_name: Name of the component
            key: Environment variable name
            value: Environment variable value (for tracking/documentation)
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        self.components[component_name].derived_env_vars[key] = value
        logger.debug(f"Added derived env var {key} to component {component_name}")

    def add_secret_env_var(
        self, component_name: str, env_name: str, secret_name: str, secret_key: str | None = None
    ) -> None:
        """
        Add an environment variable that comes from a secret.

        Args:
            component_name: Name of the component
            env_name: Name of the environment variable
            secret_name: Name of the secret containing the value
            secret_key: Key within the secret (defaults to env_name if None)
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        env_ref = EnvVarReference(name=env_name, secret_name=secret_name, key=secret_key or env_name)

        self.components[component_name].secret_env_vars[env_name] = env_ref
        logger.debug(f"Added secret env var {env_name} from secret {secret_name} to component {component_name}")

    async def get_secret_env_var_value(self, component_name: str, env_name: str, kubectl_connector=None) -> str | None:
        """
        Retrieve the actual value of a secret-based environment variable using Kubernetes API.

        Args:
            component_name: Name of the component
            env_name: Name of the environment variable
            kubectl_connector: Optional kubectl connector (if None, creates one)

        Returns:
            Environment variable value from the secret, or None if not found/error
        """
        if component_name not in self.components:
            logger.error(f"Component '{component_name}' not found")
            return None

        if env_name not in self.components[component_name].secret_env_vars:
            logger.error(f"Secret env var '{env_name}' not found in component '{component_name}'")
            return None

        env_ref = self.components[component_name].secret_env_vars[env_name]
        namespace = self.components[component_name].namespace

        if not namespace:
            logger.error(f"No namespace set for component '{component_name}'")
            return None

        try:
            if kubectl_connector is None:
                from opi.connectors.kubectl import create_kubectl_connector

                kubectl_connector = create_kubectl_connector()

            # Get secret value from Kubernetes
            secret_value = await kubectl_connector.get_secret_value(
                secret_name=env_ref.secret_name, key=env_ref.key, namespace=namespace
            )

            if secret_value:
                logger.debug(f"Retrieved secret value for {env_name} from {env_ref.secret_name}")
                return secret_value
            else:
                logger.warning(f"Secret value not found for {env_name} in {env_ref.secret_name}")
                return None

        except Exception as e:
            logger.error(f"Error retrieving secret value for {env_name}: {e}")
            return None

    def add_env_from_secret(self, component_name: str, secret_name: str) -> None:
        """
        Add a secret name to be used with envFrom for a component.

        Args:
            component_name: Name of the component
            secret_name: Name of the secret to use with envFrom
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        if secret_name not in self.components[component_name].env_from_secrets:
            self.components[component_name].env_from_secrets.append(secret_name)
            logger.debug(f"Added envFrom secret {secret_name} to component {component_name}")

    def add_web_address(self, component_name: str, address: str) -> None:
        """
        Add a web address (domain/URL) for a component.

        Args:
            component_name: Name of the component
            address: Web address/domain
        """
        logger.info(f"ConfigHandler.add_web_address called with component='{component_name}', address='{address}'")
        if component_name not in self.components:
            logger.error(
                f"ConfigHandler: Component '{component_name}' not found. Available components: {list(self.components.keys())}"
            )
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        if address not in self.components[component_name].web_addresses:
            self.components[component_name].web_addresses.append(address)
            logger.info(f"ConfigHandler: Successfully added web address '{address}' to component '{component_name}'")

    def add_storage_config(self, component_name: str, storage_type: str, config: dict[str, Any]) -> None:
        """
        Add storage configuration for a component.

        Args:
            component_name: Name of the component
            storage_type: Type of storage (e.g., 'pvc', 's3', 'volume')
            config: Storage configuration details
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        self.components[component_name].storage[storage_type] = config
        logger.debug(f"Added storage config ({storage_type}) to component {component_name}")

    def add_secret(self, component_name: str, secret_name: str) -> None:
        """
        Add a secret reference for a component.

        Args:
            component_name: Name of the component
            secret_name: Name of the secret
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        if secret_name not in self.components[component_name].secrets:
            self.components[component_name].secrets.append(secret_name)
            logger.debug(f"Added secret {secret_name} to component {component_name}")

    def add_argocd_resource(self, component_name: str, resource_name: str) -> None:
        """
        Add an ArgoCD resource reference for a component.

        Args:
            component_name: Name of the component
            resource_name: Name of the ArgoCD resource
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        if resource_name not in self.components[component_name].argocd_resources:
            self.components[component_name].argocd_resources.append(resource_name)
            logger.debug(f"Added ArgoCD resource {resource_name} to component {component_name}")

    def add_custom_config(self, component_name: str, key: str, value: Any) -> None:
        """
        Add custom configuration for a component.

        Args:
            component_name: Name of the component
            key: Configuration key
            value: Configuration value
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        self.components[component_name].custom_config[key] = value
        logger.debug(f"Added custom config {key} to component {component_name}")

    def add_sso_config(self, component_name: str, sso_config: dict[str, Any]) -> None:
        """
        Add SSO configuration for a component.

        Args:
            component_name: Name of the component
            sso_config: SSO configuration details
        """
        if component_name not in self.components:
            raise KeyError(
                f"Component '{component_name}' not found. Components must be added with add_component() first."
            )

        self.components[component_name].sso_config.update(sso_config)
        logger.debug(f"Added SSO config to component {component_name}")

    def add_keycloak_config(
        self, component_name: str, realm_name: str, client_id: str, client_secret_encrypted: str, discovery_url: str
    ) -> None:
        """
        Add Keycloak SSO configuration for a component.

        Args:
            component_name: Name of the component
            realm_name: Keycloak realm name
            client_id: OIDC client ID
            client_secret_encrypted: Encrypted client secret
            discovery_url: OIDC discovery URL
        """
        keycloak_config = {
            "type": "keycloak",
            "realm": realm_name,
            "client_id": client_id,
            "client_secret_encrypted": client_secret_encrypted,
            "discovery_url": discovery_url,
        }

        self.add_sso_config(component_name, keycloak_config)

        # OIDC environment variables are now handled through secrets only
        # All three OIDC variables (CLIENT_ID, CLIENT_SECRET, DISCOVERY_URL)
        # will be provided via secret references in the deployment manifest

        logger.debug(f"Added Keycloak SSO config for component {component_name}")

    async def get_all_env_vars_with_values(
        self, component_name: str, kubectl_connector=None, resolve_secrets: bool = False
    ) -> dict[str, str]:
        """
        Get all environment variables for a component with their values.

        Args:
            component_name: Name of the component
            kubectl_connector: Optional kubectl connector for secret resolution
            resolve_secrets: If True, retrieve actual values from secrets via Kubernetes API

        Returns:
            Dictionary of environment variable name -> value
        """
        if component_name not in self.components:
            logger.error(f"Component '{component_name}' not found")
            return {}

        component = self.components[component_name]
        all_env_vars = {}

        # Add direct environment variables
        all_env_vars.update(component.env_vars)

        # Add derived environment variables (these have values)
        all_env_vars.update(component.derived_env_vars)

        # Handle secret-based environment variables
        if resolve_secrets:
            for env_name, env_ref in component.secret_env_vars.items():
                try:
                    secret_value = await self.get_secret_env_var_value(component_name, env_name, kubectl_connector)
                    if secret_value is not None:
                        all_env_vars[env_name] = secret_value
                    else:
                        all_env_vars[env_name] = f"<secret:{env_ref.secret_name}:{env_ref.key}>"
                except Exception as e:
                    logger.warning(f"Failed to resolve secret env var {env_name}: {e}")
                    all_env_vars[env_name] = f"<secret:{env_ref.secret_name}:{env_ref.key}:ERROR>"
        else:
            # Show secret references without actual values
            for env_name, env_ref in component.secret_env_vars.items():
                all_env_vars[env_name] = f"<secret:{env_ref.secret_name}:{env_ref.key}>"

        return all_env_vars

    def set_global_config(self, key: str, value: Any) -> None:
        """
        Set global project configuration.

        Args:
            key: Configuration key
            value: Configuration value
        """
        self.global_config[key] = value
        logger.debug(f"Set global config: {key}")

    def to_dict(self) -> dict[str, Any]:
        """
        Convert configuration to dictionary format.

        Returns:
            Dictionary representation of the configuration
        """
        logger.info(f"ConfigHandler.to_dict called. Components in handler: {list(self.components.keys())}")
        config = {
            "project_name": self.project_name,
            "generation_timestamp": None,  # Will be set when serializing
            "global_config": self.global_config,
            "components": {},
        }

        for name, component in self.components.items():
            logger.info(f"ConfigHandler: Processing component '{name}' with namespace '{component.namespace}'")
            component_dict = {
                "name": component.name,
                "type": component.type,
            }

            if component.namespace:
                component_dict["namespace"] = component.namespace

            # Always include these fields even if empty to provide complete overview
            component_dict["environment_variables"] = component.env_vars
            component_dict["derived_environment_variables"] = component.derived_env_vars

            # Convert secret env vars to a serializable format
            secret_env_vars_dict = {}
            for env_name, env_ref in component.secret_env_vars.items():
                secret_env_vars_dict[env_name] = {
                    "secret_name": env_ref.secret_name,
                    "secret_key": env_ref.key,
                    "type": "secret_reference",
                }
            component_dict["secret_environment_variables"] = secret_env_vars_dict

            component_dict["env_from_secrets"] = component.env_from_secrets
            component_dict["web_addresses"] = component.web_addresses

            if component.storage:
                component_dict["storage"] = component.storage

            if component.secrets:
                component_dict["secrets"] = component.secrets

            if component.argocd_resources:
                component_dict["argocd_resources"] = component.argocd_resources

            if component.custom_config:
                component_dict["custom_config"] = component.custom_config

            if component.sso_config:
                component_dict["sso_config"] = component.sso_config

            config["components"][name] = component_dict

        logger.info(f"ConfigHandler: Final config has {len(config['components'])} components")
        return config

    def to_yaml(self) -> str:
        """
        Convert configuration to YAML format.

        Returns:
            YAML string representation of the configuration
        """
        from datetime import datetime

        config = self.to_dict()
        config["generation_timestamp"] = datetime.now().isoformat()

        yaml = YAML()
        yaml.default_flow_style = False
        yaml.preserve_quotes = True
        yaml.width = 4096
        from io import StringIO

        stream = StringIO()
        yaml.dump(config, stream)
        return stream.getvalue()

    def save_to_file(self, file_path: str) -> None:
        """
        Save configuration to a YAML file.

        Args:
            file_path: Path where to save the configuration file
        """
        logger.info(
            f"ConfigHandler.save_to_file called with path='{file_path}', components count: {len(self.components)}"
        )
        try:
            yaml_content = self.to_yaml()
            logger.info(f"ConfigHandler: Generated YAML content length: {len(yaml_content)} characters")
            with open(file_path, "w") as f:
                f.write(yaml_content)
            logger.info(f"ConfigHandler: Successfully saved configuration to: {file_path}")
        except Exception as e:
            logger.error(f"ConfigHandler: Failed to save configuration to {file_path}: {e}")
            raise


def create_configuration_handler(project_name: str, project_data: dict[str, Any]) -> ConfigurationHandler:
    """
    Factory function to create a ConfigurationHandler instance.

    Args:
        project_name: Name of the project
        project_data: Original project configuration data

    Returns:
        ConfigurationHandler instance
    """
    return ConfigurationHandler(project_name, project_data)
