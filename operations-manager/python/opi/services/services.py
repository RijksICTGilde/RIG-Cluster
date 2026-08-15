"""
Centralized service handling adapter for OPI.

This module provides a consistent interface for handling services across
the entire application, from form submission to project processing.
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import ValidationError

from opi.core.buttons import check_button_variant
from opi.services.config_lists import find_patchable_list
from opi.services.services_enums import CleanupStrategy, ServiceBinding, ServiceKind, ServiceType

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
    #: LOTC-knopvariant: "primary" | "secondary" | "warning" | "subtle" | ...
    #: Het sjabloon zet hem rechtstreeks in ``type``, en het component slaat een woord
    #: dat het niet kent stil over -- zie ``check_button_variant`` in __post_init__.
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
        check_button_variant(self.kind, f"DeploymentAction '{self.label}'")
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


def service_entry_data(entry: Any) -> Any:
    """Return the ``data`` of a service entry, format-agnostic (None if none).

    The DEFINE-side counterpart of :func:`service_entry_config`. A definition (today:
    the attachments catalog) sits under ``data`` rather than ``config``, because it is
    not configuration of a use -- it is the thing being used. Both entry shapes are
    handled: the record form (``{"name": "attachments", "data": [...]}``) and the legacy
    single-key form (``{"attachments": {"data": [...]}}``).
    """
    if not isinstance(entry, dict):
        return None
    if "name" in entry or "reference" in entry:
        return entry.get("data")
    name = service_entry_name(entry)
    body = entry.get(name) if name is not None else None
    if isinstance(body, dict):
        return body.get("data")
    return None


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
    its metadata, binding, variables, and optional configurations.
    """

    name: str
    description: str
    icon: str
    color: str
    binding: ServiceBinding
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


class _ServiceDefinitionsView(Mapping[ServiceType, ServiceDefinition]):
    """Read-only view on the definitions the service packages declare (RC-36).

    The definitions live in ``opi.services.registry``, assembled from each service's
    own package. This module cannot import that registry at module level -- the
    packages import *this* module for ``ServiceDefinition`` -- so the lookup is
    deferred to first use. A plain ``Mapping`` keeps every existing call site
    (``[...]``, ``.get``, ``.items()``, ``.values()``, iteration) working unchanged.
    """

    def _source(self) -> dict[ServiceType, ServiceDefinition]:
        from opi.services.registry import SERVICE_DEFINITIONS

        return SERVICE_DEFINITIONS

    def __getitem__(self, key: ServiceType) -> ServiceDefinition:
        return self._source()[key]

    def __iter__(self) -> Iterator[ServiceType]:
        return iter(self._source())

    def __len__(self) -> int:
        return len(self._source())


class ServiceAdapter:
    """
    Adapter for handling service operations and mappings.

    This class provides a centralized way to handle service definitions,
    mappings, and operations throughout the application.
    """

    #: Every service's metadata, keyed by service type. Each service declares its own
    #: ``ServiceDefinition`` in its own package (RC-36); this is the assembled view.
    SERVICE_DEFINITIONS: ClassVar[Mapping[ServiceType, ServiceDefinition]] = _ServiceDefinitionsView()

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
        return definition is not None and definition.binding is ServiceBinding.COMPONENT

    @classmethod
    def is_deployment_service(cls, service: ServiceType) -> bool:
        """Check if a service is deployment-shared."""
        definition = cls.get_service_definition(service)
        return definition is not None and definition.binding is ServiceBinding.DEPLOYMENT

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
    def merge_component_service_entries(
        cls, existing: list[Any], service_names: list[str]
    ) -> list[str | dict[str, Any]]:
        """Rebuild a component's services list from names, keeping existing entries.

        Rebuilding from bare names alone (``build_component_service_entries``) silently
        drops the config an entry carries -- attachment couplings, storage mounts, a
        component-level ``tls`` -- because the PATCH body has names only. A name that is
        already present keeps its entry as it stands; only a genuinely new name gets a
        freshly built entry (storage services get their default config, as in
        add_component). Names missing from ``service_names`` fall out, along with their
        config: that is what removal means. The order follows the requested list.
        """
        kept: dict[str, Any] = {}
        for entry in existing or []:
            entry_name = service_entry_name(entry)
            if entry_name is not None and entry_name not in kept:
                kept[entry_name] = entry
        merged: list[str | dict[str, Any]] = []
        for name in service_names:
            if name in kept:
                merged.append(kept[name])
            else:
                merged.extend(cls.build_component_service_entries([name]))
        return merged

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

        Fields the platform writes into the same block (``keycloak.realms``) are carried
        over from the existing config instead of being replaced -- see
        ``_keep_platform_fields``.
        """
        cls.parse_services_from_strings([service_name])  # rejects an unknown service name
        target_list = cls._resolve_target_services_list(
            project_data, layer, component_name=component_name, deployment_name=deployment_name, create=True
        )
        key = "name" if layer.value == "project" else "reference"

        # Configuring on a component/deployment selects the service at the project level
        # too -- but only if the service allows that (RC-84). A structural check requires
        # every component service to resolve to a project-level service
        # (project_validation), and a service that needs a project-level decision has to
        # get one instead of a blank block nobody filled in.
        if layer.value != "project":
            cls.ensure_project_selection(project_data, service_name)

        for index, entry in enumerate(target_list):
            if service_entry_name(entry) == service_name:
                config = cls._keep_platform_fields(service_name, layer, config, service_entry_config(entry))
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
    def ensure_project_selection(cls, project_data: dict[str, Any], *service_names: str) -> None:
        """Select these services at project level where they are not there yet (RC-84).

        Whether that may happen without anyone asking is each service's own answer
        (``Service.implicit_project_entry``): a service with nothing to decide at project
        level enrols itself with the entry it names, a service that needs a decision --
        which domains, which realm, an administrator's approval -- refuses, and the caller
        is told to select it at project level first.

        Never duplicates and never demotes an existing project entry: an entry already
        present (bare or with config) is left untouched. Nothing is written unless every
        name is allowed, so a rejected list leaves the project file as it was.

        Raises ``ServiceValidationError`` for an unknown service name, or when a service
        may not enrol itself.
        """
        cls.parse_services_from_strings(list(service_names))  # rejects an unknown service name

        # Lazy: the registry imports this module, so it cannot be imported at load time.
        from opi.services.registry import get_service

        services = project_data.setdefault("services", [])
        present = {service_entry_name(entry) for entry in services}
        new_entries: list[str | dict[str, Any]] = []
        refused: list[str] = []
        for service_name in service_names:
            if service_name in present:
                continue
            present.add(service_name)
            entry = get_service(ServiceType(service_name)).implicit_project_entry()
            if entry is None:
                refused.append(service_name)
            else:
                new_entries.append(entry)

        if refused:
            raise ServiceValidationError(
                f"Services that must be enabled at project level first: {refused}. They need project-level "
                f"configuration that cannot be assumed, so they are not added automatically."
            )
        services.extend(new_entries)

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

        Fields the platform writes are not the caller's to clear either, so a block that
        holds them keeps exactly those and loses the rest -- "reset my settings" must not
        mean "throw away the realm-admin password". See ``_keep_platform_fields``.
        """
        target_list = cls._resolve_target_services_list(
            project_data, layer, component_name=component_name, deployment_name=deployment_name, create=False
        )
        for index, entry in enumerate(target_list):
            if service_entry_name(entry) == service_name:
                if isinstance(entry, str):
                    return False  # already bare -- no config to remove
                kept = cls._platform_fields_of(service_name, layer, service_entry_config(entry))
                if kept:
                    cls.set_service_config(
                        project_data,
                        service_name,
                        layer,
                        kept,
                        component_name=component_name,
                        deployment_name=deployment_name,
                    )
                    return True
                target_list[index] = service_name
                return True
        return False

    @classmethod
    def _platform_fields_of(cls, service_name: str, layer: ConfigLayer, config: Any) -> dict[str, Any]:
        """The platform-written fields present in ``config``, or an empty dict."""
        if not isinstance(config, dict):
            return {}
        # Lazy: the registry imports this module, so it cannot be imported at load time.
        from opi.services.registry import get_service

        try:
            service = get_service(ServiceType(service_name))
        except ValueError:
            return {}
        return {key: config[key] for key in service.platform_managed_fields(layer) if key in config}

    @classmethod
    def _keep_platform_fields(
        cls, service_name: str, layer: ConfigLayer, config: dict[str, Any] | list[Any], stored: Any
    ) -> dict[str, Any] | list[Any]:
        """Carry the platform-written fields of the stored config into its replacement.

        This method replaces the whole block, so a field the caller never mentioned would
        simply disappear. For a user setting that is the intended "reset to default"; for
        ``keycloak.realms`` it destroyed the only copy of the realm-admin password.

        The API refuses a write that CARRIES such a field (422, in the route), so this is
        the guarantee for the other half: a write that leaves it out cannot lose it. The
        stored value is passed through untouched -- not re-validated and not re-dumped --
        so it keeps exactly the bytes it had, and it stays the safety net for any future
        write path that does not go through a route.
        """
        kept = cls._platform_fields_of(service_name, layer, stored)
        if not kept or not isinstance(config, dict):
            return config
        overwritten = sorted(key for key, value in kept.items() if key in config and config[key] != value)
        if overwritten:
            logger.warning(
                "Ignored a write to platform-managed field(s) %s of service '%s' at the %s layer; "
                "the stored value is kept",
                ", ".join(overwritten),
                service_name,
                layer.value,
            )
        return {**config, **kept}

    @classmethod
    def patch_service_config_list(
        cls,
        project_data: dict[str, Any],
        service_name: str,
        layer: ConfigLayer,
        *,
        add: list[Any],
        remove: list[str],
        list_field: str | None = None,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, int]:
        """Add, update or remove items in one list of a service's config at ``layer``.

        The PATCH counterpart of ``set_service_config``: instead of replacing the whole
        block, only the named items change. ``add`` takes full entries (validated against
        the service's own item model here, so a malformed entry fails before anything is
        written); an entry whose key already exists replaces it. ``remove`` takes keys
        only, and a key that is not there is a no-op -- removing twice is fine. Remove
        runs first, so a key in both lists is replaced outright.

        Which list, and what identifies one entry, comes from the config model itself
        (``opi/services/config_lists.py``). ``list_field`` is ``None`` for a config that
        IS a list (storage mounts, attachment couplings) and names the field for a config
        that CONTAINS one (``invite.active``, ``cross-domain-access.inbound``,
        ``sleep-mode.match``). In the second case the surrounding fields are carried over
        untouched -- that is the whole point: a PUT there rewrites them, and a caller who
        does not resend them wipes them.

        A list of plain values (``sleep-mode.match``) has no key field: the value IS its
        identity, so add is a set union and remove takes values.

        Writes through ``set_service_config`` afterwards, so project-level selection and
        entry normalization stay on the one path. Returns per-action counts so the
        caller can report a no-op as a no-op.
        """
        # Lazy: the registry imports this module, so it cannot be imported at load time.
        from opi.services.registry import get_service

        try:
            service_type = ServiceType(service_name)
        except ValueError:
            raise ServiceValidationError(f"Unknown service: {service_name}") from None
        service = get_service(service_type)
        model = service.config_model_for(layer)
        spec = find_patchable_list(model, list_field)
        if model is None or spec is None:
            named = f" list '{list_field}'" if list_field else ""
            raise ServiceValidationError(
                f"Service '{service_name}' has no patchable{named} config at the {layer.value} layer"
            )

        item_model = spec.item_model
        if item_model is None:
            # Plain values; validated below, through the model that owns the list (a
            # match pattern is checked by sleep-mode's own field validator, not here).
            validated_add: list[Any] = list(add)
        else:
            try:
                validated_add = [
                    item_model.model_validate(item).model_dump(by_alias=True, exclude_unset=True) for item in add
                ]
            except ValidationError as e:
                raise ServiceValidationError(f"Invalid '{service_name}' entry: {e.errors(include_url=False)}") from e

        target_list = cls._resolve_target_services_list(
            project_data, layer, component_name=component_name, deployment_name=deployment_name, create=True
        )
        current_config: Any = None
        for entry in target_list:
            if service_entry_name(entry) == service_name:
                current_config = service_entry_config(entry)
                break
        expected = dict if spec.name else list
        if current_config is not None and not isinstance(current_config, expected):
            raise ServiceValidationError(
                f"The config of '{service_name}' at the {layer.value} layer is not "
                f"{'an object' if spec.name else 'a list'}; only the PUT can replace it"
            )
        if spec.name:
            current: list[Any] = list((current_config or {}).get(spec.name) or [])
        else:
            current = list(current_config or [])

        item_key = spec.item_key

        def key_of(item: Any) -> Any:
            if item_key is None:
                return item
            return item.get(item_key) if isinstance(item, dict) else None

        removed_keys = set(remove)
        kept = [item for item in current if key_of(item) not in removed_keys]
        removed = len(current) - len(kept)

        merged: list[Any] = list(kept)
        positions = {key_of(item): index for index, item in enumerate(merged)}
        added = 0
        updated = 0
        for item in validated_add:
            item_key_value = key_of(item)
            if item_key_value in positions:
                merged[positions[item_key_value]] = item
                updated += 1
            else:
                positions[item_key_value] = len(merged)
                merged.append(item)
                added += 1

        new_config: dict[str, Any] | list[Any]
        if spec.name:
            # Everything around the patched list is carried over verbatim: not re-dumped
            # through the model, so a field this call does not touch keeps exactly the
            # value (and the spelling) it had on disk. Validated as a whole, because the
            # rules that matter here live on the owning model -- the match-pattern check
            # on sleep-mode, the unique-rule-name check on cross-domain-access.
            new_config = dict(current_config or {})
            new_config[spec.name] = merged
            try:
                model.model_validate(new_config)
            except ValidationError as e:
                raise ServiceValidationError(f"Invalid '{service_name}' config: {e.errors(include_url=False)}") from e
        else:
            new_config = merged

        cls.set_service_config(
            project_data,
            service_name,
            layer,
            new_config,
            component_name=component_name,
            deployment_name=deployment_name,
        )
        return {"added": added, "updated": updated, "removed": removed}
