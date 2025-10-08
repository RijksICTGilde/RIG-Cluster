"""
Centralized service handling adapter for OPI.

This module provides a consistent interface for handling services across
the entire application, from form submission to project processing.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from opi.services.services_enums import ServiceType

logger = logging.getLogger(__name__)


@dataclass
class VariableDefinition:
    """
    Definition of a variable provided by a service.

    This class encapsulates all information about variables that services
    provide to deployments, including descriptions, aliases, and how they
    are sourced (from secrets or generated directly as env vars).
    """

    name: str
    description: str
    source: str = "direct"  # "secret" or "direct" - how the value is provided
    aliases: list[str] = field(default_factory=list)  # Alternative names (e.g., APP_ prefixed versions)
    secret_key: str | None = None  # If source="secret", which secret class field maps to this variable

    def get_all_names(self) -> list[str]:
        """Get all possible names (primary name + aliases) for this variable."""
        return [self.name, *self.aliases]


@dataclass
class ServiceDefinition:
    """
    Definition of a service with all its properties and configuration.

    This class encapsulates all information about a service including
    its metadata, scope, variables, and optional configurations.
    """

    name: str
    description: str
    icon: str
    color: str
    scope: str  # "component" or "deployment"
    variables: list[VariableDefinition] = field(default_factory=list)
    secret_class: str | None = None
    # TODO: specific definitions should not be here
    storage_config: dict[str, Any] | None = None
    component_flag: str | None = None


class DatabaseVariables(Enum):
    """Database service variable definitions - single source of truth."""

    HOST = VariableDefinition(
        name="DATABASE_SERVER_HOST",
        description="PostgreSQL server hostnaam",
        source="secret",
        secret_key="host",
        aliases=["APP_DATABASE_SERVER_HOST", "APP_DATABASE_SERVER"],
    )
    PORT = VariableDefinition(
        name="DATABASE_SERVER_PORT",
        description="PostgreSQL server poort",
        source="secret",
        secret_key="port",
        aliases=["APP_DATABASE_PORT", "APP_DATABASE_SERVER_PORT"],
    )
    USER = VariableDefinition(
        name="DATABASE_SERVER_USER",
        description="Database gebruikersnaam",
        source="secret",
        secret_key="username",
        aliases=["APP_DATABASE_USER"],
    )
    PASSWORD = VariableDefinition(
        name="DATABASE_PASSWORD",
        description="Database gebruiker wachtwoord",
        source="secret",
        secret_key="password",
        aliases=["APP_DATABASE_PASSWORD"],
    )
    DATABASE = VariableDefinition(
        name="DATABASE_DB",
        description="Database naam",
        source="secret",
        secret_key="database",
        aliases=["APP_DATABASE_DB"],
    )
    SCHEMA = VariableDefinition(
        name="DATABASE_SCHEMA",
        description="Database schema naam",
        source="secret",
        secret_key="schema",
        aliases=["APP_DATABASE_SCHEMA"],
    )
    CONNECTION_STRING = VariableDefinition(
        name="DATABASE_SERVER_FULL",
        description="Volledige PostgreSQL connectiestring",
        source="secret",
        secret_key="connection_string",
        aliases=["APP_DATABASE_SERVER_FULL"],
    )


class KeycloakVariables(Enum):
    """Keycloak/SSO service variable definitions - single source of truth."""

    CLIENT_ID = VariableDefinition(
        name="OIDC_CLIENT_ID",
        description="OAuth2/OIDC client identificatie voor authenticatie",
        source="secret",
        secret_key="client_id",
    )
    CLIENT_SECRET = VariableDefinition(
        name="OIDC_CLIENT_SECRET",
        description="OAuth2/OIDC client geheim voor authenticatie",
        source="secret",
        secret_key="client_secret",
    )
    DISCOVERY_URL = VariableDefinition(
        name="OIDC_DISCOVERY_URL",
        description="OIDC discovery endpoint URL voor configuratie",
        source="secret",
        secret_key="discovery_url",
    )


class MinIOVariables(Enum):
    """MinIO/Object Storage service variable definitions - single source of truth."""

    URL = VariableDefinition(
        name="OBJECT_STORE_URL", description="MinIO server URL/endpoint", source="secret", secret_key="host"
    )
    USER = VariableDefinition(
        name="OBJECT_STORE_USER",
        description="MinIO toegangssleutel/gebruikersnaam",
        source="secret",
        secret_key="access_key",
    )
    PASSWORD = VariableDefinition(
        name="OBJECT_STORE_PASSWORD",
        description="MinIO geheime sleutel/wachtwoord",
        source="secret",
        secret_key="secret_key",
    )
    BUCKET_NAME = VariableDefinition(
        name="OBJECT_STORE_BUCKET_NAME", description="MinIO bucket naam", source="secret", secret_key="bucket_name"
    )
    REGION = VariableDefinition(
        name="OBJECT_STORE_REGION", description="MinIO regio configuratie", source="secret", secret_key="region"
    )


class StorageVariables(Enum):
    """Storage service variable definitions - single source of truth."""

    DATA_PATH = VariableDefinition(
        name="DATA_PATH", description="Mount pad voor permanente data opslag (/data)", source="direct"
    )
    TEMP_PATH = VariableDefinition(
        name="TEMP_PATH", description="Mount pad voor tijdelijke/tijdelijke opslag (/tmp)", source="direct"
    )


class WebVariables(Enum):
    """Web publishing service variable definitions - single source of truth."""

    PUBLIC_HOST = VariableDefinition(
        name="PUBLIC_HOST",
        description="De publieke hostname/URL waar deze component bereikbaar zal zijn",
        source="direct",
    )


class ServiceAdapter:
    """
    Adapter for handling service operations and mappings.

    This class provides a centralized way to handle service definitions,
    mappings, and operations throughout the application.
    """

    # Service definitions with their properties and variable definitions
    SERVICE_DEFINITIONS: ClassVar[dict[ServiceType, ServiceDefinition]] = {
        ServiceType.PUBLISH_ON_WEB: ServiceDefinition(
            name="Publiceren op het web",
            description="Maak de applicatie toegankelijk via het publieke internet",
            icon="wereldbol",
            color="hemelblauw",
            scope="component",
            variables=[var.value for var in WebVariables],
        ),
        ServiceType.SSO_RIJK: ServiceDefinition(
            name="Single Sign-On Rijk",
            description="Integreer met de Rijksoverheid SSO voor veilige authenticatie via Keycloak",
            icon="sleutel",
            color="groen",
            scope="component",
            secret_class="KeycloakSecret",
            variables=[var.value for var in KeycloakVariables],
        ),
        ServiceType.PERSISTENT_STORAGE: ServiceDefinition(
            name="Permanente opslag",
            description="Gegevens blijven bewaard tijdens de levenscyclus van de applicatie",
            icon="server",
            color="paars",
            scope="component",
            storage_config={"name": "data", "type": "persistent", "size": "1Gi", "mount-path": "/data"},
            variables=[var.value for var in StorageVariables if var.value.name == "DATA_PATH"],
        ),
        ServiceType.TEMP_STORAGE: ServiceDefinition(
            name="Tijdelijke schijfruimte",
            description="Gegevens worden niet bewaard tijdens de levenscyclus van de applicatie",
            icon="klok",
            color="oranje",
            scope="component",
            storage_config={"name": "temp", "type": "ephemeral", "size": "500Mi", "mount-path": "/tmp"},
            variables=[var.value for var in StorageVariables if var.value.name == "TEMP_PATH"],
        ),
        ServiceType.POSTGRESQL_DATABASE: ServiceDefinition(
            name="PostgreSQL Database",
            description="Database service voor applicaties",
            icon="database",
            color="donkerblauw",
            scope="deployment",
            secret_class="DatabaseSecret",
            variables=[var.value for var in DatabaseVariables],
        ),
        ServiceType.MINIO_STORAGE: ServiceDefinition(
            name="MinIO Object Storage",
            description="S3-compatible object storage voor documenten, afbeeldingen en grote bestanden",
            icon="map",
            color="rood",
            scope="deployment",
            secret_class="MinIOSecret",
            variables=[var.value for var in MinIOVariables],
        ),
    }

    @classmethod
    def get_all_services(cls) -> list[ServiceType]:
        """Get list of all available services."""
        return list(ServiceType)

    @classmethod
    def get_service_definition(cls, service: ServiceType) -> ServiceDefinition:
        """Get the definition for a specific service."""
        return cls.SERVICE_DEFINITIONS[service]

    @classmethod
    def get_service_by_value(cls, value: str) -> ServiceType:
        """Get a service enum by its string value."""
        return ServiceType(value)

    @classmethod
    def is_component_service(cls, service: ServiceType) -> bool:
        """Check if a service is component-specific."""
        definition = cls.get_service_definition(service)
        return definition is not None and definition.scope == "component"

    @classmethod
    def is_deployment_service(cls, service: ServiceType) -> bool:
        """Check if a service is deployment-shared."""
        definition = cls.get_service_definition(service)
        return definition is not None and definition.scope == "deployment"

    @classmethod
    def get_component_flag(cls, service: ServiceType) -> str | None:
        """Get the component flag name for a service if it has one."""
        definition = cls.get_service_definition(service)
        return definition.component_flag if definition is not None else None

    @classmethod
    def get_storage_config(cls, service: ServiceType) -> dict[str, Any] | None:
        """Get storage configuration for a storage service."""
        definition = cls.get_service_definition(service)
        return definition.storage_config if definition is not None else None

    @classmethod
    def filter_component_services(cls, services: list[ServiceType]) -> list[ServiceType]:
        """Filter services to only include component-specific ones."""
        return [service for service in services if cls.is_component_service(service)]

    @classmethod
    def filter_deployment_services(cls, services: list[ServiceType]) -> list[ServiceType]:
        """Filter services to only include deployment-shared ones."""
        return [service for service in services if cls.is_deployment_service(service)]

    @classmethod
    def get_storage_services(cls, services: list[ServiceType]) -> list[ServiceType]:
        """Filter services to only include storage services."""
        storage_services = [ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE]
        return [service for service in services if service in storage_services]

    @classmethod
    def create_storage_configs(cls, services: list[ServiceType]) -> list[dict[str, Any]]:
        """Create storage configurations for the given services."""
        storage_configs: list[dict[str, Any]] = []
        for service in cls.get_storage_services(services):
            storage_config = cls.get_storage_config(service)
            if storage_config:
                storage_configs.append(storage_config)
        return storage_configs

    @classmethod
    def parse_services_from_strings(cls, service_strings: list[str]) -> list[ServiceType]:
        """Parse service strings into ServiceType enums."""
        services: list[ServiceType] = []
        for service_str in service_strings:
            service = cls.get_service_by_value(service_str)
            if service:
                services.append(service)
            else:
                logger.warning(f"Skipping unknown service: {service_str}")
        return services

    @classmethod
    def needs_database_access(cls, services: list[ServiceType]) -> bool:
        """Check if any service requires database access."""
        return ServiceType.POSTGRESQL_DATABASE in services

    @classmethod
    def needs_object_storage(cls, services: list[ServiceType]) -> bool:
        """Check if any service requires object storage."""
        return ServiceType.MINIO_STORAGE in services

    @classmethod
    def get_variables(cls, service: ServiceType) -> list[VariableDefinition]:
        """Get the list of variable definitions provided by a service."""
        definition = cls.get_service_definition(service)
        return definition.variables if definition is not None else []

    @classmethod
    def get_variable_names(cls, service: ServiceType) -> list[str]:
        """Get all variable names (including aliases) provided by a service."""
        variables = cls.get_variables(service)
        all_names: list[str] = []
        for var in variables:
            all_names.extend(var.get_all_names())
        return all_names

    @classmethod
    def get_variables_by_source(cls, service: ServiceType, source: str) -> list[VariableDefinition]:
        """Get variables filtered by their source type ('secret' or 'direct')."""
        variables = cls.get_variables(service)
        return [var for var in variables if var.source == source]

    @classmethod
    def get_secret_variables(cls, service: ServiceType) -> list[VariableDefinition]:
        """Get variables that come from secrets."""
        return cls.get_variables_by_source(service, "secret")

    @classmethod
    def get_direct_variables(cls, service: ServiceType) -> list[VariableDefinition]:
        """Get variables that are provided directly as environment variables."""
        return cls.get_variables_by_source(service, "direct")

    @classmethod
    def get_secret_class(cls, service: ServiceType) -> str | None:
        """Get the secret class name for a service if it uses secrets."""
        definition = cls.get_service_definition(service)
        return definition.secret_class if definition is not None else None

    @classmethod
    def uses_secrets(cls, service: ServiceType) -> bool:
        """Check if a service uses secrets for any of its variables."""
        return bool(cls.get_secret_variables(service))

    @classmethod
    def uses_direct_variables(cls, service: ServiceType) -> bool:
        """Check if a service provides direct environment variables."""
        return bool(cls.get_direct_variables(service))
