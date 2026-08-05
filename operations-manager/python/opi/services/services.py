"""
Centralized service handling adapter for OPI.

This module provides a consistent interface for handling services across
the entire application, from form submission to project processing.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from opi.services.services_enums import CleanupStrategy, ServiceKind, ServiceScope, ServiceType

if TYPE_CHECKING:
    from opi.services.catalog.base import ConfigLayer

logger = logging.getLogger(__name__)


class ServiceValidationError(ValueError):
    """Raised for user-facing service validation failures."""


@dataclass
class DeploymentAction:
    """A deployment-level action button a service contributes to the UI.

    ``section-deployment-actions.html.j2`` renders one button per action. This is the
    generic hook so a service (sleep-mode's wake/sleep toggle, the database console,
    the job runner) owns its own button instead of the template deriving the condition
    itself.

    An action either POSTs (``endpoint``) or opens the shared modal shell on a fragment
    URL (``modal_endpoint`` + ``modal_title``) -- exactly one of the two.
    """

    label: str
    icon: str
    #: ROOS button kind: "primary" | "secondary" | "warning" | "subtle" | ...
    kind: str
    #: Web-route path the POST targets (CSRF handled by the template).
    endpoint: str | None = None
    #: Web-route path whose HTML is loaded into the shared edit-modal shell.
    modal_endpoint: str | None = None
    #: Heading for that modal; required with ``modal_endpoint``.
    modal_title: str | None = None
    #: Optional confirm dialog text; None means no confirmation.
    confirm_message: str | None = None
    #: Whether the button should render for this deployment.
    visible: bool = True

    def __post_init__(self) -> None:
        if bool(self.endpoint) == bool(self.modal_endpoint):
            raise ValueError(f"DeploymentAction '{self.label}' needs exactly one of endpoint / modal_endpoint")
        if self.modal_endpoint and not self.modal_title:
            raise ValueError(f"DeploymentAction '{self.label}' has a modal_endpoint but no modal_title")


#: A service's action provider: given (project_data, deployment_name) it returns the
#: deployment-level buttons that service wants shown. Kept as a plain callable so
#: services.py stays free of forms/web imports.
ActionsProvider = Callable[[dict[str, Any], str], list[DeploymentAction]]


def service_entry_name(entry: Any) -> str | None:
    """Return the service name from a ``services``-list entry, format-agnostic.

    Handles every form a services list may hold (RC-5 A):
    - bare string: ``"publish-on-web"``
    - new record (project): ``{"name": "keycloak", "config": {...}}``
    - new record (component reference): ``{"reference": "keycloak", "config": ...}``
    - legacy single-key dict: ``{"keycloak": {"config": {...}}}``

    Returns None for an unrecognisable entry. The ``name``/``reference`` keys take
    precedence, so a two-key record is handled where the legacy single-key logic
    (``next(iter(dict))``) would break.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        name = entry.get("name") or entry.get("reference")
        if name is not None:
            return name
        # Legacy: the service name is the sole key (excluding record metadata).
        keys = [key for key in entry if key not in ("config", "schema-version")]
        if len(keys) == 1:
            return keys[0]
    return None


def service_entry_schema_version(entry: Any) -> str | None:
    """Return the ``schema-version`` stamped on a service entry, or None.

    The version is a sibling of ``config`` on the entry record
    (``{"name": "keycloak", "config": {...}, "schema-version": "2.0"}``). It tells
    the provider which config version the stored block is at, so ``validate_config``
    can migrate it forward before validating. None means the entry predates
    versioning (treated as the service's current version).
    """
    if isinstance(entry, dict):
        version = entry.get("schema-version")
        if version is not None:
            return str(version)
    return None


def service_entry_type(entry: Any) -> str | None:
    """Return the ``type`` of a service entry, format-agnostic (None if none).

    ``type`` marks a service as externally provided (e.g. keycloak ``type: external``)
    and sits next to ``config``, not inside it. New record: on the entry itself. Legacy
    ``{X: {type: ..., config: ...}}``: inside the name-keyed body, which is why this
    cannot simply read the top level the way ``service_entry_schema_version`` does.
    """
    if not isinstance(entry, dict):
        return None
    if "name" in entry or "reference" in entry:
        value = entry.get("type")
        return str(value) if value is not None else None
    name = service_entry_name(entry)
    body = entry.get(name) if name is not None else None
    if isinstance(body, dict) and body.get("type") is not None:
        return str(body["type"])
    return None


def service_entry_body(entry: Any, name: str | None = None) -> Any:
    """Return the config-carrying sub-dict of a service entry, format-agnostic.

    New record (``{name/reference: X, config: ...}``) carries ``config`` and its
    siblings on the entry itself, so the body IS the entry. Legacy
    ``{X: {config: ...}}`` carries them under the service-name key.

    The returned dict is the live sub-dict, not a copy: writing to it writes to the
    entry, in whichever form that entry happens to use. That is what lets a caller
    merge into an entry without first knowing its shape. *name* is accepted as a
    hint; when omitted it is derived with ``service_entry_name``.
    """
    if not isinstance(entry, dict):
        return None
    if "name" in entry or "reference" in entry:
        return entry
    key = name if name is not None else service_entry_name(entry)
    return entry.get(key) if key is not None else None


def service_entry_config(entry: Any) -> Any:
    """Return the ``config`` of a service entry, format-agnostic (None if none).

    New record: the ``config`` field on the entry. Legacy ``{X: {config: ...}}``: the
    ``config`` under the name key, or -- for services whose legacy value carries the
    config inline without a ``config`` wrapper (e.g. metrics-scraper
    ``{metrics-scraper: {port, path}}``) -- that inline body itself.
    """
    if not isinstance(entry, dict):
        return None
    if "name" in entry or "reference" in entry:
        return entry.get("config")
    name = service_entry_name(entry)
    body = entry.get(name) if name is not None else None
    if isinstance(body, dict):
        return body.get("config", body) if "config" in body else body
    return body


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
    scope: ServiceScope
    variables: list[VariableDefinition] = field(default_factory=list)
    secret_class: str | None = None
    # TODO: specific definitions should not be here
    storage_config: dict[str, Any] | None = None
    component_flag: str | None = None
    hidden: bool = False
    kind: ServiceKind = ServiceKind.USER
    """Whether a project chooses this service (``USER``) or the platform always runs it
    (``SYSTEM``). Distinct from ``hidden``: ``hidden`` means "not in the service picker"
    (a namespace variant OPI selects itself), ``SYSTEM`` means "always on, never in the
    project file". A ``SYSTEM`` service is also kept out of the picker."""
    help_template: str | None = None
    """Optional Jinja2 template name (relative to ``templates/help/``) with a
    long-form explanation shown in a popup when the user clicks the info icon."""
    requires: list[str] = field(default_factory=list)
    """Service requirements using path syntax.

    Each entry is a yaml_path that must exist in the form data:
    - ``services/keycloak`` - the keycloak service must be selected
    - ``services/keycloak/config/restrict-access`` - this config
      path must be present

    Used for both UI behavior (auto-select, lock) and submit-time
    validation.
    """
    cleanup_strategy: CleanupStrategy = CleanupStrategy.NONE
    """How server-side resources are cleaned up when the service is removed.

    - ``NONE``      - no server-side resources to clean up (e.g. storage PVCs,
                       ingress config).  This is the default.
    - ``IMMEDIATE``  - ephemeral / easily recreatable resources are deleted
                       right away (e.g. Redis ACL users, Keycloak clients).
    - ``DEFERRED``   - persistent data resources are marked for deferred
                       deletion so they can be recovered (e.g. databases,
                       MinIO buckets).
    """
    backup_label: str | None = None
    """Short label used to identify this service in backup/restore flows.

    When set, this service is considered backupable.  Multiple service types
    can share the same label (e.g. ``POSTGRESQL_DATABASE`` and
    ``NAMESPACE_POSTGRESQL_DATABASE`` both use ``"database"``).
    The label is used as the ``resource_type`` value in backup runs and
    as the form field value in the backup wizard.
    """
    actions_provider: ActionsProvider | None = None
    """Optional provider of deployment-level action buttons (see ``DeploymentAction``).

    The deployment-actions template collects these across the services a project
    uses, so a service owns its own button instead of the template hardcoding the
    condition. ``None`` means the service contributes no buttons.
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
    USER_RO = VariableDefinition(
        name="DATABASE_SERVER_USER_RO",
        description="Read-only database gebruikersnaam",
        source="secret",
        secret_key="ro_username",
        aliases=["APP_DATABASE_USER_RO"],
    )
    PASSWORD_RO = VariableDefinition(
        name="DATABASE_PASSWORD_RO",
        description="Read-only database gebruiker wachtwoord",
        source="secret",
        secret_key="ro_password",
        aliases=["APP_DATABASE_PASSWORD_RO"],
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


def reserved_database_variable_names() -> set[str]:
    """Every env-variable name the database service already exposes (names + aliases).

    An extra schema's ``DATABASE_SCHEMA_{POSTFIX}`` variable must not collide with one
    of these (a postfix ``db`` would produce ``DATABASE_SCHEMA_DB``, uncomfortably close
    to ``DATABASE_DB``). The save-time enforcer checks a candidate against this set.
    """
    names: set[str] = set()
    for var in DatabaseVariables:
        names.add(var.value.name)
        names.update(var.value.aliases)
    return names


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
    PUBLIC_CLIENT_ID = VariableDefinition(
        name="OIDC_PUBLIC_CLIENT_ID",
        description="Public OAuth2/OIDC client identificatie voor browser-based authenticatie (keycloak-js)",
        source="secret",
        secret_key="public_client_id",
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
    HOSTNAME = VariableDefinition(
        name="OIDC_HOSTNAME",
        description="Keycloak hostname zonder scheme (afgeleid van base_url)",
        source="secret",
        secret_key="hostname",
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
        description=(
            "Prefix voor Redis-sleutels en -kanalen van dit project, zonder scheidingsteken. "
            "Bouw je sleutels en kanalen als <prefix>:<naam>. De ACL geeft alleen toegang "
            "tot sleutels en kanalen die met <prefix>: beginnen"
        ),
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


class MetricsScraperVariables(Enum):
    """Metrics scraper service variable definitions."""

    AUTH_TOKEN = VariableDefinition(
        name="METRICS_AUTH_TOKEN",
        description="Bearer token that Prometheus sends when scraping /metrics. Validate this to restrict access.",
        source="secret",
        secret_key="token",
        aliases=["PROMETHEUS_METRICS_AUTH_TOKEN"],
    )


class PlatformVariables(Enum):
    """Platform-provided variable definitions - always available in every deployment."""

    DEPLOYMENT_NAME = VariableDefinition(
        name="DEPLOYMENT_NAME",
        description="Naam van het huidige deployment",
        source="secret",
        secret_key="deployment_name",
    )
    COMPONENT_NAME = VariableDefinition(
        name="COMPONENT_NAME",
        description="Naam van het huidige component",
        source="secret",
        secret_key="component_name",
    )


class WebVariables(Enum):
    """Web publishing service variable definitions - single source of truth."""

    PUBLIC_HOST = VariableDefinition(
        name="PUBLIC_HOST",
        description="De publieke hostname/URL waar een component bereikbaar zal zijn",
        source="direct",
    )
    PUBLIC_HOSTNAME = VariableDefinition(
        name="PUBLIC_HOSTNAME",
        description="De publieke hostname (zonder scheme) waar een component bereikbaar zal zijn",
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
            scope=ServiceScope.COMPONENT,
            variables=[var.value for var in WebVariables],
        ),
        ServiceType.KEYCLOAK: ServiceDefinition(
            name="Keycloak Authentication",
            description="Configureerbare Keycloak authenticatie met ondersteuning voor SSO en lokale gebruikers",
            icon="sleutel",
            color="groen",
            scope=ServiceScope.COMPONENT,
            secret_class="KeycloakSecret",
            variables=[var.value for var in KeycloakVariables],
            requires=["services/publish-on-web"],
            cleanup_strategy=CleanupStrategy.IMMEDIATE,
        ),
        ServiceType.PERSISTENT_STORAGE: ServiceDefinition(
            name="Permanente opslag",
            description="Gegevens blijven bewaard tijdens de levenscyclus van de applicatie",
            icon="server",
            color="grijs-600",
            scope=ServiceScope.COMPONENT,
            backup_label="pvc",
            storage_config={"name": "data", "type": "persistent", "size": "1Gi", "mount-path": "/data"},
            variables=[var.value for var in StorageVariables if var.value.name == "DATA_PATH"],
            cleanup_strategy=CleanupStrategy.DEFERRED,
        ),
        ServiceType.TEMP_STORAGE: ServiceDefinition(
            name="Tijdelijke schijfruimte",
            description="Gegevens worden niet bewaard tijdens de levenscyclus van de applicatie",
            icon="klok",
            color="oranje",
            scope=ServiceScope.COMPONENT,
            storage_config={"name": "temp", "type": "ephemeral", "size": "500Mi", "mount-path": "/tmp"},
            variables=[var.value for var in StorageVariables if var.value.name == "TEMP_PATH"],
        ),
        ServiceType.POSTGRESQL_DATABASE: ServiceDefinition(
            name="PostgreSQL Database",
            description="Database service voor applicaties",
            icon="database",
            color="donkerblauw",
            scope=ServiceScope.DEPLOYMENT,
            secret_class="DatabaseSecret",
            variables=[var.value for var in DatabaseVariables],
            cleanup_strategy=CleanupStrategy.DEFERRED,
            backup_label="database",
        ),
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE: ServiceDefinition(
            name="Namespace PostgreSQL Database",
            description="Dedicated PostgreSQL database cluster voor project",
            icon="database",
            color="donkerblauw",
            scope=ServiceScope.DEPLOYMENT,
            secret_class="DatabaseSecret",
            variables=[var.value for var in DatabaseVariables],
            hidden=True,
            cleanup_strategy=CleanupStrategy.DEFERRED,
            backup_label="database",
        ),
        ServiceType.MINIO_STORAGE: ServiceDefinition(
            name="MinIO Object Storage",
            description="S3-compatible object storage voor documenten, afbeeldingen en grote bestanden",
            icon="map",
            color="rood",
            scope=ServiceScope.DEPLOYMENT,
            secret_class="MinIOSecret",
            variables=[var.value for var in MinIOVariables],
            cleanup_strategy=CleanupStrategy.DEFERRED,
            backup_label="minio",
        ),
        ServiceType.REDIS: ServiceDefinition(
            name="Redis Cache",
            description="Shared Redis cache en message broker voor caching en Celery task queues",
            icon="zandloper",
            color="rood",
            scope=ServiceScope.DEPLOYMENT,
            secret_class="RedisSecret",
            variables=[var.value for var in RedisVariables],
            cleanup_strategy=CleanupStrategy.IMMEDIATE,
        ),
        ServiceType.NAMESPACE_REDIS: ServiceDefinition(
            name="Namespace Redis Cache",
            description="Dedicated Redis instance per namespace voor caching en Celery task queues",
            icon="zandloper",
            color="rood",
            scope=ServiceScope.DEPLOYMENT,
            secret_class="RedisSecret",
            variables=[var.value for var in RedisVariables],
            hidden=True,
            cleanup_strategy=CleanupStrategy.IMMEDIATE,
        ),
        ServiceType.PLATFORM: ServiceDefinition(
            name="Platform",
            description="Automatisch beschikbare platform variabelen",
            icon="info",
            color="grijs-600",
            scope=ServiceScope.COMPONENT,
            secret_class="PlatformSecret",
            variables=[var.value for var in PlatformVariables],
            # Always on, never chosen by a project -> a system service. kind=SYSTEM
            # also keeps it out of the picker, so an explicit hidden is not needed.
            kind=ServiceKind.SYSTEM,
        ),
        ServiceType.ATTACHMENTS: ServiceDefinition(
            name="Bijlagen",
            description="Geuploade bestanden (bijv. certificaten) gekoppeld als bestand of env-var aan een component",
            icon="map",
            color="grijs-600",
            scope=ServiceScope.COMPONENT,
            variables=[],
        ),
        ServiceType.AUTHORIZATION_WALL: ServiceDefinition(
            name="Authorization Wall",
            description="OAuth2-proxy sidecar die Keycloak OIDC authenticatie afdwingt voor webapplicaties",
            icon="schild-met-vinkje-erop",
            color="groen",
            scope=ServiceScope.COMPONENT,
            help_template="authorization-wall.html.j2",
            variables=[],
            requires=[
                "services/publish-on-web",
                "services/keycloak",
                "services/keycloak/config/restrict-access",
            ],
        ),
        ServiceType.METRICS_SCRAPER: ServiceDefinition(
            name="Prometheus Metrics Scraper",
            description="Zorgt dat prometheus scraping op het component wordt ingeschakeld",
            icon="grafiek",
            color="hemelblauw",
            scope=ServiceScope.COMPONENT,
            variables=[v.value for v in MetricsScraperVariables],
        ),
        ServiceType.SLEEP_MODE: ServiceDefinition(
            name="Slaapstand",
            description=(
                "Zet bepaalde deployments, op basis van matching, na een deadline in slaapstand "
                "en wek ze op verzoek weer op. De deployment doet een koude start."
            ),
            icon="klok",
            color="donkerblauw",
            scope=ServiceScope.DEPLOYMENT,
            # Selectable in the wizard with its own project-level config section
            # (SleepModeService.config_form_section). A cluster-wide default still
            # applies, and `match` scopes which deployments it affects.
            variables=[],
            # actions_provider is bound by opi/services/catalog/sleep_mode/__init__.py
            # (the wake button); services.py must not import the catalog package.
        ),
        ServiceType.INVITE: ServiceDefinition(
            name="Uitnodiging",
            description=(
                "Nodig gebruikers uit voor het Keycloak-realm van dit project via een deelbare link. "
                "De link is de enige toegangsdrempel: wie hem heeft kan een account aanmaken. "
                "Vereist de Keycloak-service."
            ),
            icon="envelop",
            color="lichtblauw",
            # scope is not meaningful here (an invite provisions nothing), but the field is
            # required; "component" matches keycloak/attachments.
            scope=ServiceScope.COMPONENT,
            variables=[],
            # Path-syntax requirement: auto-selects keycloak, locks it in the UI, and validates
            # at submit that keycloak is present. An invite assigns a realm role, so keycloak
            # must exist. Do NOT build a second dependency mechanism next to this.
            requires=["services/keycloak"],
        ),
        ServiceType.RESOURCE_TUNING: ServiceDefinition(
            name="Resource tuning",
            description=(
                "Systeemdienst: houdt draaiende deployments in de gaten na een sync en hoogt "
                "het geheugen op van een component dat OOM'd. Draait altijd, is niet kiesbaar."
            ),
            icon="grafiek",
            color="grijs-600",
            scope=ServiceScope.DEPLOYMENT,
            variables=[],
            # Always on, never in the project file -> a system service (kind=SYSTEM also
            # keeps it out of the picker, so no explicit hidden is needed).
            kind=ServiceKind.SYSTEM,
        ),
        ServiceType.CROSS_DOMAIN_ACCESS: ServiceDefinition(
            name="Cross-domain toegang",
            description=(
                "Bepaal welke andere projecten, deployments of componenten de pods van dit "
                "project mogen bereiken en waar dit project zelf heen mag, telkens op een "
                "expliciet benoemde poort. Dit gaat over netwerktoegang tussen projecten, "
                "niet over DNS-domeinen."
            ),
            icon="netwerk",
            color="donkerblauw",
            # The rules apply per deployment (each gets its own NetworkPolicy); the effect
            # lives entirely in generated manifests, so there is nothing server-side to clean
            # up -- the generic manifest prune removes the policy files when the service is off.
            scope=ServiceScope.DEPLOYMENT,
            variables=[],
            cleanup_strategy=CleanupStrategy.NONE,
        ),
        ServiceType.USER_ENV_VARS: ServiceDefinition(
            name="Eigen omgevingsvariabelen",
            description=(
                "Systeemdienst: de eigen omgevingsvariabelen van een component, per component "
                "en per deployment-component. Draait altijd, is niet kiesbaar - elk component "
                "heeft ze. De waarden worden versleuteld opgeslagen."
            ),
            icon="instellingen",
            color="grijs-600",
            scope=ServiceScope.COMPONENT,
            variables=[],
            # Always present, never in the project file's services list -> a system
            # service (kind=SYSTEM also keeps it out of the picker).
            kind=ServiceKind.SYSTEM,
        ),
        ServiceType.ALIASES: ServiceDefinition(
            name="Aliassen",
            description=(
                "Systeemdienst: koppelt platform-variabelen aan de namen die een component "
                "verwacht (POSTGRES_HOST=$DATABASE_SERVER_HOST). Draait altijd, is niet "
                "kiesbaar. Een onbekende verwijzing is hier een harde fout, anders dan bij "
                "een eigen omgevingsvariabele."
            ),
            icon="instellingen",
            color="grijs-600",
            scope=ServiceScope.COMPONENT,
            variables=[],
            kind=ServiceKind.SYSTEM,
        ),
        ServiceType.HEALTH_CHECK: ServiceDefinition(
            name="Health check",
            description=(
                "Bepaalt hoe Kubernetes de gezondheid van het component controleert (scheme, poort en "
                "paden). Zonder deze service wordt het component ook gecontroleerd, maar alleen op "
                "TCP-niveau op de eerste inbound-poort. Kies deze service om een HTTP(S)-probe op een "
                "aparte poort en paden te richten, of om probes uit te zetten met scheme: none."
            ),
            icon="stethoscoop",
            color="rood",
            scope=ServiceScope.COMPONENT,
            variables=[],
        ),
    }

    @classmethod
    def resolve_service_dependencies(cls, selected: list[Any]) -> list[Any]:
        """Add missing service-level dependencies to a list of selected services.

        Entries may be bare service names (str) or single-key dicts carrying config
        (e.g. ``{"keycloak": {...}}`` or ``{"attachments": {"data": ...}}``); dict
        entries are preserved as-is. Only resolves ``services/X`` requires
        (single-level paths). Config-level requirements are not resolved here.

        Returns a new list with missing dependency names prepended, preserving order.
        """

        selected_set = {name for entry in selected if (name := service_entry_name(entry)) is not None}
        to_add: list[str] = []
        for entry in selected:
            svc_name = service_entry_name(entry)
            if svc_name is None:
                continue
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
        return definition is not None and definition.scope is ServiceScope.COMPONENT

    @classmethod
    def is_deployment_service(cls, service: ServiceType) -> bool:
        """Check if a service is deployment-shared."""
        definition = cls.get_service_definition(service)
        return definition is not None and definition.scope is ServiceScope.DEPLOYMENT

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
    def get_backupable_labels(cls) -> list[dict[str, str]]:
        """Get unique backup labels with display metadata from backupable services.

        Returns a list of dicts with keys: label, name, color - one per unique
        backup_label.  Order is stable (follows SERVICE_DEFINITIONS insertion order).
        """
        seen: set[str] = set()
        result: list[dict[str, str]] = []
        for definition in cls.SERVICE_DEFINITIONS.values():
            if definition.backup_label and definition.backup_label not in seen:
                seen.add(definition.backup_label)
                result.append(
                    {
                        "label": definition.backup_label,
                        "name": definition.name,
                        "color": definition.color,
                    }
                )
        return result

    @classmethod
    def get_service_types_for_backup_label(cls, backup_label: str) -> list[str]:
        """Get all service type values that share the given backup_label."""
        return [
            svc_type.value
            for svc_type, definition in cls.SERVICE_DEFINITIONS.items()
            if definition.backup_label == backup_label
        ]

    @classmethod
    def get_cleanable_service_types(cls) -> list[ServiceType]:
        """Get all service types that have server-side resources requiring cleanup."""
        return [
            svc_type
            for svc_type, definition in cls.SERVICE_DEFINITIONS.items()
            if definition.cleanup_strategy is not CleanupStrategy.NONE
        ]

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
    def build_component_service_entries(cls, service_names: list[str]) -> list[str | dict[str, Any]]:
        """Build a component-level services list with storage configs embedded.

        Converts a flat list of service name strings into the uniform component
        format where storage services carry their config as a reference record::

            ["publish-on-web", {"reference": "persistent-storage", "config": [...]}]
        """
        parsed = cls.parse_services_from_strings(service_names)
        storage_configs = cls.create_storage_configs(parsed)

        storage_by_svc: dict[str, list[dict[str, Any]]] = {}
        for cfg in storage_configs:
            svc_name = (
                ServiceType.PERSISTENT_STORAGE.value
                if cfg.get("type") == "persistent"
                else ServiceType.TEMP_STORAGE.value
            )
            storage_by_svc.setdefault(svc_name, []).append({k: v for k, v in cfg.items() if k != "type"})

        entries: list[str | dict[str, Any]] = []
        for svc in parsed:
            if svc.value in storage_by_svc:
                entries.append({"reference": svc.value, "config": storage_by_svc[svc.value]})
            else:
                entries.append(svc.value)
        return entries

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
            if not isinstance(service_item, str | dict):
                raise TypeError(f"Invalid service item type {type(service_item)}, must be str or dict: {service_item}")
            name = service_entry_name(service_item)
            if name is None:
                raise ValueError(f"Cannot determine service name from entry: {service_item}")
            service_names.append(name)

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
        # service_entry_name resolves all three entry formats. Matching on the raw dict
        # keys only saw the legacy single-key form, so a namespace service carrying
        # config (which makes it a {name, config} record) went undetected.
        return any(service_entry_name(entry) in namespace_services for entry in project_services)

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

        Pure data-manipulation logic - no I/O or git operations.

        Args:
            project_data: The mutable project configuration dict.
            service_names: Services to add (e.g. ``["postgresql-database"]``).
            component_names: Optional component names whose ``services``
                list should also be updated. If *None* or empty the services
                are only added at the project level.

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

        # Optionally update component services
        if component_names:
            # Build new entries in v2 mixed format
            new_entries = cls.build_component_service_entries(all_service_names)

            for comp_name in component_names:
                comp = existing_components[comp_name]
                existing_comp_services: list[str | dict[str, Any]] = comp.get("services", [])
                existing_comp_svc_names = set(cls.extract_service_names_from_project_services(existing_comp_services))

                entries_to_add = [
                    entry for entry in new_entries if service_entry_name(entry) not in existing_comp_svc_names
                ]

                if entries_to_add:
                    existing_comp_services.extend(entries_to_add)
                    comp["services"] = existing_comp_services
                    components_updated.append(comp_name)

        return {
            "services_added": services_added,
            "services_skipped": services_skipped,
            "components_updated": components_updated,
            "warnings": warnings,
        }

    # --- unified service-config CRUD core (RC-12 follow-up) ---------------------
    # The pure data-manipulation behind the unified ``/api/v2/.../services/{svc}``
    # endpoint: it upserts (or removes) one service's config block at a target
    # layer, leaving validation to the save chokepoint. It writes the same
    # ``{name, config}`` (project) / ``{reference, config}`` (component / deployment
    # / deployment-component) records the wizard writes, resolves identity with
    # ``service_entry_name`` and promotes a bare-string selection in place instead
    # of appending a duplicate (a services list is a selection set -- checklist
    # item 5). ``ConfigLayer`` is compared by its ``.value`` so this module need
    # not import ``catalog.base`` at runtime (that module imports this one).

    @classmethod
    def _resolve_target_services_list(
        cls,
        project_data: dict[str, Any],
        layer: ConfigLayer,
        *,
        component_name: str | None,
        deployment_name: str | None,
        create: bool,
    ) -> list[str | dict[str, Any]]:
        """Return the ``services`` list at ``layer`` (project / component /
        deployment / deployment-component), creating it when ``create`` is set.

        Raises ``ServiceValidationError`` when a name required by the layer is
        missing or does not resolve to an existing component/deployment.
        """
        container = cls._resolve_target_container(
            project_data, layer, component_name=component_name, deployment_name=deployment_name
        )
        services = container.get("services")
        if services is None:
            if not create:
                return []
            services = []
            container["services"] = services
        return services

    @classmethod
    def _resolve_target_container(
        cls,
        project_data: dict[str, Any],
        layer: ConfigLayer,
        *,
        component_name: str | None,
        deployment_name: str | None,
    ) -> dict[str, Any]:
        """Return the dict that owns the ``services`` list for ``layer``."""
        lv = layer.value
        if lv == "project":
            return project_data
        if lv == "component":
            return cls._require_named(
                project_data.get("components", []), component_name, kind="component", param="component_name"
            )
        if lv == "deployment":
            return cls._require_named(
                project_data.get("deployments", []), deployment_name, kind="deployment", param="deployment_name"
            )
        if lv == "deployment-component":
            deployment = cls._require_named(
                project_data.get("deployments", []), deployment_name, kind="deployment", param="deployment_name"
            )
            return cls._require_named(
                deployment.get("components", []),
                component_name,
                kind="deployment component",
                param="component_name",
            )
        raise ServiceValidationError(f"Unknown config target layer: {layer!r}")

    @classmethod
    def _require_named(cls, items: list[dict[str, Any]], name: str | None, *, kind: str, param: str) -> dict[str, Any]:
        """Find an item by name/reference or raise a clear ServiceValidationError."""
        if not name:
            raise ServiceValidationError(f"A '{param}' is required to target the {kind} layer")
        for item in items:
            if service_entry_name(item) == name:
                return item
        raise ServiceValidationError(f"{kind.capitalize()} '{name}' not found in project")

    @classmethod
    def set_service_config(
        cls,
        project_data: dict[str, Any],
        service_name: str,
        layer: ConfigLayer,
        config: dict[str, Any] | list[Any],
        *,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> None:
        """Upsert one service's ``config`` block at ``layer`` (pure data-manipulation).

        Mirrors ``add_services_to_project``: no I/O and no schema validation -- the
        caller persists through ``save_and_commit_project``, which runs
        ``validate_service_configs`` and rejects a config the service's model does
        not accept. An existing entry (bare string, ``{name}``/``{reference}``
        record, or legacy single-key dict) is found via ``service_entry_name`` and
        replaced in place; a ``schema-version``/``type`` sibling is preserved. The
        record key is ``name`` at the project layer and ``reference`` elsewhere,
        matching the shape the wizard writes.
        """
        cls.parse_services_from_strings([service_name])  # rejects an unknown service name
        target_list = cls._resolve_target_services_list(
            project_data, layer, component_name=component_name, deployment_name=deployment_name, create=True
        )
        key = "name" if layer.value == "project" else "reference"

        # Configuring on a component/deployment implicitly selects the service at the
        # project level, so the caller need not add it to the root services list
        # first. A structural check requires every component service to resolve to a
        # project-level service (project_validation); this keeps a component-only
        # config write self-contained. If the service genuinely needs project-level
        # config, the bare selection fails validation there -- a clear signal, not a
        # silent gap.
        if layer.value != "project":
            cls._ensure_project_selection(project_data, service_name)

        for index, entry in enumerate(target_list):
            if service_entry_name(entry) == service_name:
                record: dict[str, Any] = {key: service_name, "config": config}
                schema_version = service_entry_schema_version(entry)
                if schema_version is not None:
                    record["schema-version"] = schema_version
                entry_type = service_entry_type(entry)
                if entry_type is not None:
                    record["type"] = entry_type
                target_list[index] = record
                return

        target_list.append({key: service_name, "config": config})

    @classmethod
    def _ensure_project_selection(cls, project_data: dict[str, Any], service_name: str) -> None:
        """Add ``service_name`` as a bare project-level selection if absent.

        Never duplicates and never demotes an existing configured project entry --
        an entry already present (bare or with config) is left untouched.
        """
        services = project_data.setdefault("services", [])
        if any(service_entry_name(entry) == service_name for entry in services):
            return
        services.append(service_name)

    @classmethod
    def remove_service_config(
        cls,
        project_data: dict[str, Any],
        service_name: str,
        layer: ConfigLayer,
        *,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> bool:
        """Remove one service's config at ``layer`` by demoting its entry to a bare
        string, keeping the selection. Returns True if an entry was changed, False
        if the service was not present at that layer.

        Demotion (rather than deleting the entry) is the least-surprising CRUD
        semantics for a config resource: DELETE clears the config, not the fact that
        the component/project uses the service.
        """
        target_list = cls._resolve_target_services_list(
            project_data, layer, component_name=component_name, deployment_name=deployment_name, create=False
        )
        for index, entry in enumerate(target_list):
            if service_entry_name(entry) == service_name:
                if isinstance(entry, str):
                    return False  # already bare -- no config to remove
                target_list[index] = service_name
                return True
        return False
