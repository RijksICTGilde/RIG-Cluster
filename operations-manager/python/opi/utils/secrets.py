"""
Centralized secrets configuration and management for OPI.

This module provides a type-safe, declarative way to define and manage
Kubernetes secrets with consistent key mappings and validation.
"""

import logging
from abc import ABC
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar

from opi.connectors.kubectl import KubectlConnector
from opi.services import ServiceType
from opi.services.services import ServiceAdapter

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseSecret")


@dataclass
class SecretContext:
    """Context for secret operations."""

    namespace: str
    prefix: str  # deployment_name, component_name, etc.
    kubectl_connector: KubectlConnector
    manifest_generator: Any


class BaseSecret(ABC):
    """Base class for all secret types with generic create/get methods."""

    # Each subclass defines these
    SECRET_NAME_TEMPLATE: ClassVar[str] = ""
    SERVICE_TYPE: ClassVar[ServiceType]

    def __post_init__(self) -> None:
        """Default post-initialization hook. Subclasses can override for custom validation."""

    def _get_service_variables(self) -> list[Any]:
        return ServiceAdapter.get_service_definition(self.SERVICE_TYPE).variables

    def to_k8s_secret_data(self) -> dict[str, str]:
        """Convert dataclass fields to Kubernetes secret key-value pairs including aliases."""
        secret_data: dict[str, str] = {}

        # Get all variables from service definition
        variables = self._get_service_variables()

        for var_def in variables:
            if var_def.source == "secret" and var_def.secret_key:
                value_str = str(getattr(self, var_def.secret_key))

                # Add main key
                secret_data[var_def.name] = value_str

                # Add all aliases
                for alias in var_def.aliases:
                    secret_data[alias] = value_str

        # Allow subclasses to add computed/additional keys
        additional_keys = self._get_additional_keys()
        secret_data.update(additional_keys)

        return secret_data

    def _get_additional_keys(self) -> dict[str, str]:
        """Override in subclasses to add computed or additional keys."""
        return {}

    @classmethod
    def from_k8s_secret_data(cls: type[T], secret_data: dict[str, str]) -> T:
        """Create instance from Kubernetes secret data using service definitions."""
        kwargs = {}

        if cls.SERVICE_TYPE is None:
            # This method should only be called on concrete subclasses, not the abstract base class
            return cls(**kwargs)

        service_def = ServiceAdapter.get_service_definition(cls.SERVICE_TYPE)
        variables = service_def.get("variables", [])

        for var_def in variables:
            if var_def.source == "secret":
                # Try main key first, then aliases
                k8s_keys_to_try = [var_def.name] + var_def.aliases

                for k8s_key in k8s_keys_to_try:
                    if k8s_key in secret_data:
                        value = secret_data[k8s_key]
                        # Handle type conversion if needed
                        value = cls._convert_field_value(var_def.secret_key, value)
                        kwargs[var_def.secret_key] = value
                        break

        # This method should only be called on concrete subclasses, not the abstract base class
        return cls(**kwargs)

    @classmethod
    def _convert_field_value(cls, field_name: str, value: str) -> str | int:
        """Override in subclasses for field-specific type conversion."""
        return value

    @classmethod
    def get_secret_name(cls, prefix: str) -> str:
        """Generate secret name using prefix."""
        return cls.SECRET_NAME_TEMPLATE.format(prefix=prefix)

    @classmethod
    async def get(cls: type[T], context: SecretContext) -> T | None:
        """Retrieve secret from Kubernetes."""
        secret_name = cls.get_secret_name(context.prefix)
        logger.debug(f"Retrieving secret {secret_name} from namespace {context.namespace}")

        secret_data = await context.kubectl_connector.get_secret(secret_name, context.namespace)

        if not secret_data:
            logger.debug(f"Secret {secret_name} not found in namespace {context.namespace}")
            return None

        return cls.from_k8s_secret_data(secret_data)

    @classmethod
    async def get_data(cls: type[T], kubectl_connector: KubectlConnector, namespace: str, prefix: str) -> T | None:
        """Retrieve secret data directly with kubectl connector and return typed object."""
        secret_name = cls.get_secret_name(prefix)
        logger.debug(f"Retrieving secret {secret_name} from namespace {namespace}")

        secret_data = await kubectl_connector.get_secret(secret_name, namespace)

        if not secret_data:
            logger.debug(f"Secret {secret_name} not found in namespace {namespace}")
            return None

        return cls.from_k8s_secret_data(secret_data)

    def to_config_data(self) -> dict[str, str]:
        """
        Convert to configuration data using only main keys (no aliases).
        Used for encrypted project configuration storage.
        """
        if self.SERVICE_TYPE is None:
            # For UserSecret and other custom secrets, fall back to current behavior
            return self.to_k8s_secret_data()

        config_data: dict[str, str] = {}
        variables = self._get_service_variables()

        # Only include main variable names (not aliases)
        for var_def in variables:
            if var_def.source == "secret" and hasattr(self, var_def.secret_key):
                value = getattr(self, var_def.secret_key)
                config_data[var_def.name] = str(value)

        return config_data


@dataclass
class DatabaseSecret(BaseSecret):
    """Database secret configuration using service definitions."""

    host: str
    port: int
    username: str
    password: str
    database: str
    schema: str

    SECRET_NAME_TEMPLATE: ClassVar[str] = "{prefix}-database"
    SERVICE_TYPE: ClassVar[ServiceType] = ServiceType.POSTGRESQL_DATABASE

    def __post_init__(self) -> None:
        """Validate database secret data."""
        super().__post_init__()  # Call parent's default implementation

    @property
    def connection_string(self) -> str:
        """Generate PostgreSQL connection string."""
        return f"postgresql://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}?options=--search_path%3D{self.schema}"

    def _get_additional_keys(self) -> dict[str, str]:
        """Add computed keys that are not defined in service definitions."""
        return {
            # Connection string - this is computed and not in service definition
            "DATABASE_SERVER_FULL": self.connection_string,
            "APP_DATABASE_SERVER_FULL": self.connection_string,
        }

    @classmethod
    def _convert_field_value(cls, field_name: str, value: str) -> str | int:
        """Convert port to integer."""
        if field_name == "port":
            return int(value)
        return value


@dataclass
class MinIOSecret(BaseSecret):
    """MinIO/Object Store secret configuration using service definitions."""

    host: str
    access_key: str
    secret_key: str
    bucket_name: str
    region: str = "us-east-1"  # Default region - will be overridden by settings.MINIO_REGION

    SECRET_NAME_TEMPLATE: ClassVar[str] = "{prefix}-minio"
    SERVICE_TYPE: ClassVar[ServiceType] = ServiceType.MINIO_STORAGE

    def __post_init__(self) -> None:
        """Validate MinIO secret data."""
        super().__post_init__()  # Call parent's default implementation


@dataclass
class KeycloakSecret(BaseSecret):
    """Keycloak/OIDC secret configuration."""

    client_id: str
    client_secret: str
    discovery_url: str

    SECRET_NAME_TEMPLATE: ClassVar[str] = "{prefix}-keycloak"
    SERVICE_TYPE: ClassVar[ServiceType] = ServiceType.SSO_RIJK

    def __post_init__(self) -> None:
        """Validate Keycloak secret data."""
        super().__post_init__()  # Call parent's default implementation


@dataclass
class UserSecret(BaseSecret):
    """User-defined environment variables secret."""

    env_vars: dict[str, str]

    SECRET_NAME_TEMPLATE: ClassVar[str] = "{prefix}-user"

    def __post_init__(self) -> None:
        """Validate user secret data."""
        super().__post_init__()  # Call parent's default implementation

    def to_k8s_secret_data(self) -> dict[str, str]:
        """For user secrets, the env_vars dict is the secret data."""
        return self.env_vars.copy()

    @classmethod
    def from_k8s_secret_data(cls, secret_data: dict[str, str]) -> "UserSecret":
        """Create from all secret data as env_vars."""
        return cls(env_vars=secret_data.copy())
