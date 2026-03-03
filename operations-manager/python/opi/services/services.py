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


class ServiceValidationError(ValueError):
    """Raised for user-facing service validation failures."""


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
    hidden: bool = False
    help_template: str | None = None
    """Optional Jinja2 template name (relative to ``templates/help/``) with a
    long-form explanation shown in a popup when the user clicks the info icon."""
    requires: list[str] = field(default_factory=list)
    """Service requirements using path syntax.

    Each entry is a yaml_path that must exist in the form data:
    - ``services/keycloak`` — the keycloak service must be selected
    - ``services/keycloak/config/restrict-access`` — this config
      path must be present

    Used for both UI behavior (auto-select, lock) and submit-time
    validation.
    """


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
    URL = VariableDefinition(
        name="OIDC_URL",
        description="Keycloak basis URL",
        source="secret",
        secret_key="base_url",
    )
    REALM = VariableDefinition(
        name="OIDC_REALM",
        description="Keycloak realm naam",
        source="secret",
        secret_key="realm",
    )


class MinIOVariables(Enum):
    """MinIO/Object Storage service variable definitions - single source of truth."""

    HOST = VariableDefinition(
        name="OBJECT_STORE_HOST",
        description="MinIO server hostname",
        source="secret",
        secret_key="host",
        aliases=["APP_OBJECT_STORE_HOST"],
    )
    PORT = VariableDefinition(
        name="OBJECT_STORE_PORT",
        description="MinIO server port",
        source="secret",
        secret_key="port",
        aliases=["APP_OBJECT_STORE_PORT"],
    )
    # URL and ENDPOINT_URL are computed in MinIOSecret._get_additional_keys()
    USER = VariableDefinition(
        name="OBJECT_STORE_USER",
        description="MinIO toegangssleutel/gebruikersnaam",
        source="secret",
        secret_key="access_key",
        aliases=["APP_OBJECT_STORE_USER"],
    )
    PASSWORD = VariableDefinition(
        name="OBJECT_STORE_PASSWORD",
        description="MinIO geheime sleutel/wachtwoord",
        source="secret",
        secret_key="secret_key",
        aliases=["APP_OBJECT_STORE_PASSWORD"],
    )
    BUCKET_NAME = VariableDefinition(
        name="OBJECT_STORE_BUCKET_NAME",
        description="MinIO bucket naam",
        source="secret",
        secret_key="bucket_name",
        aliases=["APP_OBJECT_STORE_BUCKET_NAME"],
    )
    REGION = VariableDefinition(
        name="OBJECT_STORE_REGION",
        description="MinIO regio configuratie",
        source="secret",
        secret_key="region",
        aliases=["APP_OBJECT_STORE_REGION"],
    )


class StorageVariables(Enum):
    """Storage service variable definitions - single source of truth."""

    DATA_PATH = VariableDefinition(
        name="DATA_PATH", description="Mount pad voor permanente data opslag (/data)", source="direct"
    )
    TEMP_PATH = VariableDefinition(
        name="TEMP_PATH", description="Mount pad voor tijdelijke/tijdelijke opslag (/tmp)", source="direct"
    )


class RedisVariables(Enum):
    """Redis cache service variable definitions - single source of truth."""

    HOST = VariableDefinition(
        name="REDIS_HOST",
        description="Redis server hostname",
        source="secret",
        secret_key="host",
        aliases=["APP_REDIS_HOST"],
    )
    PORT = VariableDefinition(
        name="REDIS_PORT",
        description="Redis server port",
        source="secret",
        secret_key="port",
        aliases=["APP_REDIS_PORT"],
    )
    USERNAME = VariableDefinition(
        name="REDIS_USERNAME",
        description="Redis ACL username",
        source="secret",
        secret_key="username",
        aliases=["APP_REDIS_USERNAME"],
    )
    PASSWORD = VariableDefinition(
        name="REDIS_PASSWORD",
        description="Redis password",
        source="secret",
        secret_key="password",
        aliases=["APP_REDIS_PASSWORD"],
    )
    PREFIX = VariableDefinition(
        name="REDIS_PREFIX",
        description="Redis key/channel prefix for this project",
        source="secret",
        secret_key="key_prefix",
        aliases=["APP_REDIS_PREFIX"],
    )
    URL = VariableDefinition(
        name="REDIS_URL",
        description="Full Redis connection URL",
        source="secret",
        secret_key="url",
        aliases=["APP_REDIS_URL"],
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
        ServiceType.KEYCLOAK: ServiceDefinition(
            name="Keycloak Authentication",
            description="Configureerbare Keycloak authenticatie met ondersteuning voor SSO en lokale gebruikers",
            icon="sleutel",
            color="groen",
            scope="component",
            secret_class="KeycloakSecret",
            variables=[var.value for var in KeycloakVariables],
            requires=["services/publish-on-web"],
        ),
        ServiceType.PERSISTENT_STORAGE: ServiceDefinition(
            name="Permanente opslag",
            description="Gegevens blijven bewaard tijdens de levenscyclus van de applicatie",
            icon="server",
            color="grijs-600",
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
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE: ServiceDefinition(
            name="Namespace PostgreSQL Database",
            description="Dedicated PostgreSQL database cluster voor project",
            icon="database",
            color="donkerblauw",
            scope="deployment",
            secret_class="DatabaseSecret",
            variables=[var.value for var in DatabaseVariables],
            hidden=True,
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
        ServiceType.REDIS: ServiceDefinition(
            name="Redis Cache",
            description="Shared Redis cache en message broker voor caching en Celery task queues",
            icon="zandloper",
            color="rood",
            scope="deployment",
            secret_class="RedisSecret",
            variables=[var.value for var in RedisVariables],
        ),
        ServiceType.NAMESPACE_REDIS: ServiceDefinition(
            name="Namespace Redis Cache",
            description="Dedicated Redis instance per namespace voor caching en Celery task queues",
            icon="zandloper",
            color="rood",
            scope="deployment",
            secret_class="RedisSecret",
            variables=[var.value for var in RedisVariables],
            hidden=True,
        ),
        ServiceType.AUTHORIZATION_WALL: ServiceDefinition(
            name="Authorization Wall",
            description="OAuth2-proxy sidecar die Keycloak OIDC authenticatie afdwingt voor webapplicaties",
            icon="schild-met-vinkje-erop",
            color="groen",
            scope="component",
            help_template="authorization-wall.html.j2",
            variables=[],
            requires=[
                "services/publish-on-web",
                "services/keycloak",
                "services/keycloak/config/restrict-access",
            ],
        ),
    }

    @classmethod
    def resolve_service_dependencies(cls, selected: list[str]) -> list[str]:
        """Add missing service-level dependencies to a list of selected services.

        Only resolves ``services/X`` requires (single-level paths).
        Config-level requirements are not resolved here.

        Returns a new list with dependencies prepended before the services
        that need them, preserving original order.
        """
        selected_set = set(selected)
        to_add: list[str] = []
        for svc_name in selected:
            try:
                svc_type = ServiceType(svc_name)
            except ValueError:
                continue
            definition = cls.SERVICE_DEFINITIONS.get(svc_type)
            if not definition or not definition.requires:
                continue
            for req in definition.requires:
                if req.startswith("services/") and req.count("/") == 1:
                    dep_name = req.removeprefix("services/")
                    if dep_name not in selected_set:
                        selected_set.add(dep_name)
                        to_add.append(dep_name)
        return [*to_add, *selected]

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
    def extract_service_names_from_project_services(cls, project_services: list[str | dict]) -> list[str]:
        """
        Extract service names from project-level services list.

        Project-level services can be in two formats:
        - String: "namespace-postgresql-database"
        - Dict: {"namespace-postgresql-database": {"config": {...}}}

        Args:
            project_services: List of service strings or dicts from project.yaml

        Returns:
            List of service name strings

        Raises:
            ValueError: If service item format is invalid
        """
        service_names: list[str] = []

        for service_item in project_services:
            if isinstance(service_item, str):
                # Simple string format
                service_names.append(service_item)
            elif isinstance(service_item, dict):
                # Dict format: {"service-name": {"config": {...}}}
                # Extract the key (service name)
                if len(service_item) == 0:
                    raise ValueError(f"Service dict is empty: {service_item}")
                if len(service_item) > 1:
                    raise ValueError(f"Service dict should have exactly one key (service name): {service_item}")
                service_name = next(iter(service_item.keys()))
                service_names.append(service_name)
            else:
                raise TypeError(f"Invalid service item type {type(service_item)}, must be str or dict: {service_item}")

        return service_names

    @classmethod
    def parse_services_from_strings(cls, service_names: list[str]) -> list[ServiceType]:
        """
        Parse service names into ServiceType enums.

        Components reference services by name only. Service configurations
        are defined at the project level in the 'services:' section.

        Args:
            service_names: List of service name strings

        Returns:
            List of ServiceType enums

        Raises:
            ValueError: If service name is unknown
        """
        services: list[ServiceType] = []

        for service_name in service_names:
            if not isinstance(service_name, str):
                raise TypeError(f"Service name must be a string, got {type(service_name)}: {service_name}")

            try:
                service = cls.get_service_by_value(service_name)
                services.append(service)
            except ValueError:
                # Provide helpful error message for renamed service
                if service_name == "sso-rijk":
                    raise ServiceValidationError(
                        "Service 'sso-rijk' has been renamed to 'keycloak'. "
                        "Please update your project.yaml to use 'keycloak' instead."
                    ) from None
                raise ServiceValidationError(f"Unknown service: {service_name}") from None

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
    def needs_redis(cls, services: list[ServiceType]) -> bool:
        """Check if any service requires Redis cache."""
        return ServiceType.REDIS in services or ServiceType.NAMESPACE_REDIS in services

    @classmethod
    def needs_infrastructure_namespace(cls, services: list[ServiceType]) -> bool:
        """Check if any service requires a dedicated infrastructure namespace."""
        namespace_services = {ServiceType.NAMESPACE_POSTGRESQL_DATABASE, ServiceType.NAMESPACE_REDIS}
        return any(svc in namespace_services for svc in services)

    @classmethod
    def project_uses_infrastructure_namespace(cls, project_data: dict) -> bool:
        """
        Check if a project uses any service that requires an infrastructure namespace.

        Args:
            project_data: The project configuration data

        Returns:
            True if the project uses namespace-postgresql-database or namespace-redis
        """
        namespace_services = {
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
            ServiceType.NAMESPACE_REDIS.value,
        }
        project_services = project_data.get("services", [])
        for service_item in project_services:
            if isinstance(service_item, str):
                if service_item in namespace_services:
                    return True
            elif isinstance(service_item, dict) and any(svc in service_item for svc in namespace_services):
                return True
        return False

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

    @classmethod
    def add_services_to_project(
        cls,
        project_data: dict[str, Any],
        service_names: list[str],
        component_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Add one or more services (and their dependencies) to a project's configuration.

        Pure data-manipulation logic — no I/O or git operations.

        Args:
            project_data: The mutable project configuration dict.
            service_names: Services to add (e.g. ``["postgresql-database"]``).
            component_names: Optional component names whose ``uses-services``
                should also be updated. If *None* or empty the services are only
                added at the project level.

        Returns:
            Result dict with keys ``services_added``, ``services_skipped``,
            ``components_updated``, and ``warnings``.

        Raises:
            ValueError: If a service name is unknown or a component name
                does not exist in the project.
        """
        # Validate all service names
        cls.parse_services_from_strings(service_names)

        # Resolve dependencies (returns deps first, then the services themselves)
        all_service_names = cls.resolve_service_dependencies(service_names)

        # Determine which services already exist at the project level
        existing_service_names = set(cls.extract_service_names_from_project_services(project_data.get("services", [])))

        services_added: list[str] = []
        services_skipped: list[str] = []
        warnings: list[str] = []

        for svc in all_service_names:
            if svc in existing_service_names:
                services_skipped.append(svc)
                warnings.append(f"Service '{svc}' already exists on the project")
            else:
                services_added.append(svc)

        # Validate component names before mutating project data
        components_updated: list[str] = []
        if component_names:
            existing_components = {comp.get("name"): comp for comp in project_data.get("components", [])}
            invalid_components = [c for c in component_names if c not in existing_components]
            if invalid_components:
                raise ServiceValidationError(f"Components not found in project: {invalid_components}")

        # Append new services to the project-level list
        if "services" not in project_data:
            project_data["services"] = []
        project_data["services"].extend(services_added)

        # Optionally update component uses-services
        if component_names:
            # Pre-compute storage configs once for all components
            parsed_services = cls.parse_services_from_strings(all_service_names)
            storage_configs = cls.create_storage_configs(parsed_services)

            for comp_name in component_names:
                comp = existing_components[comp_name]
                comp_services = comp.get("uses-services", [])
                comp_updated = False

                for svc in all_service_names:
                    if svc not in comp_services:
                        comp_services.append(svc)
                        comp_updated = True

                if comp_updated:
                    comp["uses-services"] = comp_services
                    components_updated.append(comp_name)

                    # Add storage configs for storage services
                    if storage_configs:
                        existing_storage = comp.get("storage", [])
                        existing_storage_names = {s.get("name") for s in existing_storage}
                        for sc in storage_configs:
                            if sc.get("name") not in existing_storage_names:
                                existing_storage.append(sc)
                        if existing_storage:
                            comp["storage"] = existing_storage

        return {
            "services_added": services_added,
            "services_skipped": services_skipped,
            "components_updated": components_updated,
            "warnings": warnings,
        }
