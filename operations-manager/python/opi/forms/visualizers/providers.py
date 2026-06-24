"""
Options providers for dynamic form field population.

This module provides the OptionsProvider protocol and concrete implementations
for populating select/radio fields with dynamic data from OPI's domain.
"""

import re
from typing import Any, ClassVar, Protocol

from opi.core.cluster_config import CLUSTER_CONFIG
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType


class OptionsProvider(Protocol):
    """
    Protocol for dynamic options providers.

    Options providers are used to populate select/radio fields
    with options from external data sources (databases, configs, APIs).
    """

    def get_options(self) -> list[dict[str, Any]]:
        """
        Get options for a select/radio field.

        Returns:
            List of option dicts with 'value' and 'label' keys.
            May also include 'description', 'disabled', 'group' etc.
        """
        ...


class ClusterOptionsProvider:
    """
    Provides available Kubernetes cluster options.

    Reads from the cluster configuration to provide available
    deployment targets.
    """

    def __init__(self, include_empty: bool = False, empty_label: str = "Selecteer een cluster") -> None:
        """
        Initialize the cluster options provider.

        Args:
            include_empty: Whether to include an empty "select" option (for dropdowns)
            empty_label: Label for the empty option
        """
        self.include_empty = include_empty
        self.empty_label = empty_label

    def get_options(self) -> list[dict[str, Any]]:
        """Get available cluster options."""
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": self.empty_label})

        # Map cluster names to user-friendly labels
        cluster_labels = {
            "local": "Lokaal (Kind)",
            "odcn-staging": "Staging Cluster (ODC-Noord)",
            "odcn-production": "Productie Cluster (ODC-Noord)",
        }

        for cluster_name in CLUSTER_CONFIG:
            label = cluster_labels.get(cluster_name, cluster_name.title())
            options.append(
                {
                    "value": cluster_name,
                    "label": label,
                }
            )

        return options


class ServiceOptionsProvider:
    """
    Provides available service options.

    Reads from the ServiceAdapter to provide available
    services that can be enabled for projects.
    """

    def __init__(
        self,
        include_empty: bool = False,
        filter_scope: str | None = None,
    ) -> None:
        """
        Initialize the service options provider.

        Args:
            include_empty: Whether to include an empty "select" option
            filter_scope: Filter services by scope ("component" or "deployment")
        """
        self.include_empty = include_empty
        self.filter_scope = filter_scope

    def get_options(self) -> list[dict[str, Any]]:
        """Get available service options from ServiceAdapter definitions."""
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": "Selecteer een service"})

        for service_type in ServiceType:
            definition = ServiceAdapter.get_service_definition(service_type)

            # Skip hidden services
            if definition.hidden:
                continue

            # Filter by scope if specified
            if self.filter_scope and definition.scope != self.filter_scope:
                continue

            option: dict[str, Any] = {
                "value": service_type.value,
                "label": definition.name,
                "description": definition.description,
                "icon": definition.icon,
                "color": definition.color,
                "scope": definition.scope,
            }

            if definition.requires:
                option["requires"] = definition.requires

            if definition.help_template:
                option["help_template"] = definition.help_template

            options.append(option)

        return options


class ComponentTypeOptionsProvider:
    """Provides component type options (single, frontend, backend)."""

    def __init__(self, include_empty: bool = False) -> None:
        self.include_empty = include_empty

    def get_options(self) -> list[dict[str, Any]]:
        """Get available component type options."""
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": "Selecteer type"})

        options.extend(
            [
                {
                    "value": "single",
                    "label": "Single (All-in-one)",
                    "description": "Complete applicatie in een component",
                },
                {
                    "value": "frontend",
                    "label": "Frontend",
                    "description": "User interface component",
                },
                {
                    "value": "backend",
                    "label": "Backend",
                    "description": "API of service component",
                },
            ]
        )

        return options


class UserRoleOptionsProvider:
    """Provides user role options for project access."""

    def __init__(self, include_empty: bool = False) -> None:
        self.include_empty = include_empty

    def get_options(self) -> list[dict[str, Any]]:
        """Get available user role options."""
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": "Selecteer rol"})

        options.extend(
            [
                {
                    "value": "admin",
                    "label": "Administrator",
                    "description": "Volledige toegang tot alle resources en instellingen",
                },
                {
                    "value": "developer",
                    "label": "Developer",
                    "description": "Kan applicaties deployen en logs bekijken",
                },
                {
                    "value": "operator",
                    "label": "Operator",
                    "description": "Alleen-lezen toegang voor monitoring",
                },
            ]
        )

        return options


class CpuRequestOptionsProvider:
    """Provides CPU request options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available CPU request options."""
        return [
            {"value": "50m", "label": "50m (minimaal)"},
            {"value": "100m", "label": "100m"},
            {"value": "250m", "label": "250m"},
            {"value": "500m", "label": "500m"},
        ]


class CpuLimitOptionsProvider:
    """Provides CPU limit options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available CPU limit options."""
        return [
            {"value": "500m", "label": "500m"},
            {"value": "1", "label": "1 CPU"},
        ]


ALL_MEMORY_STEPS: list[tuple[str, str, int]] = [
    ("25Mi", "25 Mi", 25),
    ("32Mi", "32 Mi", 32),
    ("64Mi", "64 Mi", 64),
    ("96Mi", "96 Mi", 96),
    ("128Mi", "128 Mi", 128),
    ("256Mi", "256 Mi", 256),
    ("512Mi", "512 Mi", 512),
    ("768Mi", "768 Mi", 768),
    ("1Gi", "1 Gi", 1024),
    ("1536Mi", "1.5 Gi", 1536),
    ("2Gi", "2 Gi", 2048),
    ("2560Mi", "2.5 Gi", 2560),
    ("3Gi", "3 Gi", 3072),
    ("3584Mi", "3.5 Gi", 3584),
    ("4Gi", "4 Gi", 4096),
]


def get_memory_steps(max_mi: int | None = None) -> list[tuple[str, str]]:
    """Return memory steps up to the cluster's max_memory_limit_mi."""
    if max_mi is None:
        from opi.core.cluster_config import get_max_memory_limit_mi
        from opi.core.config import settings

        max_mi = get_max_memory_limit_mi(settings.CLUSTER_MANAGER)
    return [(v, lbl) for v, lbl, mi in ALL_MEMORY_STEPS if mi <= max_mi]


class MemoryOptionsProvider:
    """Provides memory options for components (used for limit fields).

    If *current_value* is set and not in the standard steps, it is inserted
    at the correct sorted position so the dropdown always contains the
    value currently stored in the project file (e.g. tuner-assigned values).
    """

    def __init__(self, current_value: str | None = None) -> None:
        self.current_value = current_value

    def _get_max_mi(self) -> int:
        from opi.core.cluster_config import get_max_memory_limit_mi
        from opi.core.config import settings

        return get_max_memory_limit_mi(settings.CLUSTER_MANAGER)

    def get_options(self) -> list[dict[str, Any]]:
        steps = get_memory_steps(max_mi=self._get_max_mi())
        options = [{"value": v, "label": lbl} for v, lbl in steps]

        if self.current_value and not any(o["value"] == self.current_value for o in options):
            from opi.services.resource_analyzer import parse_k8s_memory_to_mi

            try:
                current_mi = parse_k8s_memory_to_mi(self.current_value)
            except ValueError:
                return options

            label = f"{int(current_mi)} Mi" if current_mi == int(current_mi) else f"{current_mi:.1f} Mi"
            new_option = {"value": self.current_value, "label": label}

            # Insert at sorted position
            step_mis = [parse_k8s_memory_to_mi(v) for v, _ in steps]
            insert_idx = len(options)
            for i, step_mi in enumerate(step_mis):
                if current_mi < step_mi:
                    insert_idx = i
                    break
            options.insert(insert_idx, new_option)

        return options


class MemoryRequestOptionsProvider(MemoryOptionsProvider):
    """Provides memory options for request fields (capped lower than limits)."""

    def _get_max_mi(self) -> int:
        from opi.core.cluster_config import get_max_memory_request_mi
        from opi.core.config import settings

        return get_max_memory_request_mi(settings.CLUSTER_MANAGER)


class DomainModeOptionsProvider:
    """Provides domain mode options for URL configuration."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available domain mode options."""
        return [
            {
                "value": "component-specific",
                "label": "Component-specifiek (standaard)",
                "description": "Elk component krijgt zijn eigen unieke URL",
            },
            {
                "value": "deployment-name",
                "label": "Deployment-naam (gedeeld domein)",
                "description": "Alle componenten delen dezelfde domeinnaam met verschillende paden",
            },
            {
                "value": "custom",
                "label": "Aangepast subdomein",
                "description": "Specificeer een custom subdomein voor alle componenten",
            },
            {
                "value": "nice-url",
                "label": "Eigen subdomein (nice URL)",
                "description": "Punt-gescheiden URLs zoals frontend.mijnapp.rijks.app",
            },
        ]


class StorageTypeOptionsProvider:
    """Provides storage type options for container volumes."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available storage type options."""
        return [
            {
                "value": "persistent",
                "label": "Persistent",
                "description": "Data blijft bewaard bij herstart van de container",
            },
            {
                "value": "ephemeral",
                "label": "Tijdelijk (ephemeral)",
                "description": "Data wordt gewist bij herstart van de container",
            },
        ]


class StorageSizeOptionsProvider:
    """Provides storage size options for persistent volumes."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available storage size options."""
        return [
            {"value": "50Mi", "label": "50 MB"},
            {"value": "100Mi", "label": "100 MB"},
            {"value": "250Mi", "label": "250 MB"},
            {"value": "500Mi", "label": "500 MB"},
            {"value": "1Gi", "label": "1 GB"},
        ]


class KeycloakTemplateOptionsProvider:
    """Provides Keycloak realm template options."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available Keycloak template options."""
        return [
            {
                "value": "sso-only",
                "label": "Alleen authenticatie via SSO, geen gebruikersbeheer",
            },
            {
                "value": "sso-support",
                "label": "SSO met ondersteuning voor applicatie-specifieke configuratie (standaard)",
            },
        ]


class PullPolicyOptionsProvider:
    """Provides Kubernetes image pull policy options."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available image pull policy options."""
        return [
            {"value": "Always", "label": "Always"},
            {"value": "IfNotPresent", "label": "IfNotPresent"},
            {"value": "Never", "label": "Never"},
        ]


class BaseDomainOptionsProvider:
    """Provides base domain options for deployment URLs."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available base domain options."""
        return [
            {"value": "", "label": "Standaard (clusternaam)"},
            {"value": "rijksapp.nl", "label": "rijksapp.nl"},
            {"value": "__custom__", "label": "Eigen domein..."},
        ]


class ClusterBaseDomainOptionsProvider:
    """Provides base domain options based on the selected cluster.

    Reads supported nice-URL domains from CLUSTER_CONFIG. When no cluster
    is specified, returns all known domains across all clusters.
    """

    def __init__(self, cluster: str | None = None) -> None:
        self.cluster = cluster

    def get_options(self) -> list[dict[str, Any]]:
        """Get base domain options, filtered by cluster.

        When no cluster is explicitly provided, falls back to the
        CLUSTER_MANAGER setting (the cluster this OPI instance manages).
        """
        from opi.core.config import settings

        def _extract_domain(entry: str | dict[str, Any]) -> str:
            return entry["domain"] if isinstance(entry, dict) else entry

        cluster = self.cluster or settings.CLUSTER_MANAGER
        if cluster and cluster in CLUSTER_CONFIG:
            postfix = CLUSTER_CONFIG[cluster].get("ingress_postfix", "")
            default_label = f"Cluster standaard ({postfix.lstrip('.')})" if postfix else "Cluster standaard"
            options: list[dict[str, Any]] = [{"value": "", "label": default_label}]

            raw = CLUSTER_CONFIG[cluster].get("nice_url", {}).get("supported_domains", [])
            domains = [_extract_domain(d) for d in raw]
            options.extend({"value": d, "label": d} for d in domains)
            options.append({"value": "__custom__", "label": "Eigen domein..."})
            return options

        # Fallback: no matching cluster config - return empty with custom option
        return [{"value": "", "label": "Cluster standaard"}, {"value": "__custom__", "label": "Eigen domein..."}]


class FilteredServiceOptionsProvider:
    """
    Provides service options filtered to project-level enabled services.

    Used by component `services` checkbox group. Only shows services
    that the project has enabled (cross-part dependency).
    """

    def __init__(self, project_services: list[str] | None = None) -> None:
        self.project_services = project_services or []

    def get_options(self) -> list[dict[str, Any]]:
        """Get service options filtered to project-enabled services."""
        options: list[dict[str, Any]] = []
        for service_type in ServiceType:
            if service_type.value not in self.project_services:
                continue
            definition = ServiceAdapter.get_service_definition(service_type)
            options.append(
                {
                    "value": service_type.value,
                    "label": definition.name,
                    "description": definition.description,
                    "icon": definition.icon,
                    "color": definition.color,
                }
            )
        return options


class ComponentReferenceOptionsProvider:
    """
    Provides component names from the project as select options.

    Used by deployment component reference selects (cross-part dependency).
    """

    def __init__(
        self,
        component_names: list[str] | None = None,
        include_empty: bool = False,
        empty_label: str = "Geen root component",
        exclude_references: list[str] | None = None,
    ) -> None:
        self.component_names = component_names or []
        self.include_empty = include_empty
        self.empty_label = empty_label
        self.exclude_references = set(exclude_references or [])

    def get_options(self) -> list[dict[str, Any]]:
        """Get component name options, excluding already-used references."""
        options: list[dict[str, Any]] = []
        if self.include_empty:
            options.append({"value": "", "label": self.empty_label})
        options.extend(
            {"value": name, "label": name} for name in self.component_names if name not in self.exclude_references
        )
        return options


class RootComponentOptionsProvider(ComponentReferenceOptionsProvider):
    """ComponentReferenceOptionsProvider with an empty 'no root' option."""

    def __init__(self, component_names: list[str] | None = None) -> None:
        super().__init__(component_names=component_names, include_empty=True)


class BareDomainComponentOptionsProvider(ComponentReferenceOptionsProvider):
    """ComponentReferenceOptionsProvider for bare domain component selection.

    Shows component names with an empty 'not on bare domain' option.
    """

    def __init__(self, component_names: list[str] | None = None) -> None:
        super().__init__(
            component_names=component_names,
            include_empty=True,
            empty_label="Niet bereikbaar op kaal domein",
        )


class BackupScheduleFrequencyOptionsProvider:
    """Provides RRULE frequency options for the backup schedule select."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "", "label": "Geen"},
            {"value": "DAILY", "label": "Dagelijks"},
            {"value": "WEEKLY", "label": "Wekelijks"},
            {"value": "MONTHLY", "label": "Maandelijks"},
        ]


class BackupScheduleTimeOptionsProvider:
    """Provides half-hour time slots for the backup time indication."""

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        for hour in range(24):
            for minute in (0, 30):
                time_str = f"{hour:02d}:{minute:02d}"
                options.append({"value": time_str, "label": time_str})
        return options


class BackupScheduleDayOptionsProvider:
    """Provides day-of-week options for weekly schedules."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "MO", "label": "Maandag"},
            {"value": "TU", "label": "Dinsdag"},
            {"value": "WE", "label": "Woensdag"},
            {"value": "TH", "label": "Donderdag"},
            {"value": "FR", "label": "Vrijdag"},
            {"value": "SA", "label": "Zaterdag"},
            {"value": "SU", "label": "Zondag"},
        ]


class BackupScheduleMonthDayOptionsProvider:
    """Provides day-of-month options for monthly schedules."""

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": str(day), "label": str(day)} for day in range(1, 29)]


class BackupResourceTypesOptionsProvider:
    """Provides resource type options filtered by what the deployment actually uses.

    Uses ``yaml_data`` and ``yaml_path`` (passed generically by the bridge) to
    determine which deployment is being edited, then checks which backup-capable
    services that deployment uses via ``deployment_uses_service``.

    Works for both:
    - Schedule modal: path ``deployments[N]/backup/resource_types`` → deployment at index N
    - Manual backup: path ``resource_types`` → uses ``_cluster_deployments`` context
    """

    def __init__(
        self,
        yaml_data: dict[str, Any] | None = None,
        yaml_path: str | None = None,
    ) -> None:
        self._yaml_data = yaml_data or {}
        self._yaml_path = yaml_path or ""

    def get_options(self) -> list[dict[str, Any]]:
        from opi.handlers.project_file_handler import create_project_file_handler
        from opi.services import ServiceAdapter

        all_labels = ServiceAdapter.get_backupable_labels()
        deployment_name = self._resolve_deployment_name()
        if not deployment_name:
            return [{"value": bl["label"], "label": bl["name"]} for bl in all_labels]

        pfh = create_project_file_handler()
        filtered = pfh.get_deployment_backup_labels(self._yaml_data, deployment_name)
        return [{"value": bl["label"], "label": bl["name"]} for bl in filtered]

    def _resolve_deployment_name(self) -> str:
        """Determine the deployment name from the path or form data."""
        # Schedule modal: deployments[N]/backup/resource_types
        match = re.match(r"deployments\[(\d+)]", self._yaml_path)
        if match:
            idx = int(match.group(1))
            deployments = self._yaml_data.get("deployments", [])
            if isinstance(deployments, list) and idx < len(deployments):
                dep = deployments[idx]
                if isinstance(dep, dict):
                    return dep.get("name", "")
            return ""

        # Manual backup: selected deployment_name in form data
        selected = self._yaml_data.get("deployment_name", "")
        if selected:
            return str(selected)

        return ""


class BackupDeploymentOptionsProvider:
    """Provides deployment options for the manual backup modal.

    Reads ``_cluster_deployments`` from ``yaml_data`` (set by
    ``_build_backup_restore_context_async`` via template_data merge).
    """

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        data = yaml_data or {}
        self._deployments: list[dict[str, Any]] = data.get("_cluster_deployments", [])

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": dep["name"], "label": f"{dep['name']} ({dep.get('namespace', '')})"}
            for dep in self._deployments
            if dep.get("name")
        ]


class DeploymentCloneFromOptionsProvider:
    """Provides existing deployment names as clone-from options.

    Used when adding a new deployment to select a source deployment
    to clone data (databases, storage) from.
    """

    def __init__(self, deployment_names: list[str] | None = None) -> None:
        self.deployment_names = deployment_names or []

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = [{"value": "", "label": "Niet klonen"}]
        options.extend({"value": name, "label": name} for name in self.deployment_names)
        return options


class RepositoryOptionsProvider:
    """
    Provides repository names from the project as select options.

    Used by deployment repository selects (cross-part dependency).
    """

    def __init__(self, repository_names: list[str] | None = None) -> None:
        self.repository_names = repository_names or []

    def get_options(self) -> list[dict[str, Any]]:
        """Get repository name options."""
        return [{"value": name, "label": name} for name in self.repository_names]


class DomainFormatOptionsProvider:
    """Provides domain-format template options filtered by base_domain capabilities.

    Always shows dash-separated formats. When the selected base_domain supports
    dot-separated hostnames, the dot variants are shown as well.
    Options are sorted alphabetically by value.
    """

    _DASH_FORMATS: ClassVar[list[str]] = [
        "component-deployment-project",
        "component-deployment-subdomain",
        "component-subdomain",
        "deployment-project",
        "deployment-subdomain",
        "subdomain",
    ]

    _DOT_FORMATS: ClassVar[list[str]] = [
        "component.deployment.project",
        "component.deployment.subdomain",
        "component.subdomain",
        "deployment.project",
        "deployment.subdomain",
    ]

    def __init__(self, base_domain: str | None = None, cluster: str | None = None) -> None:
        self.base_domain = base_domain
        self.cluster = cluster

    def get_options(self) -> list[dict[str, Any]]:
        import logging

        from opi.core.cluster_config import get_domain_supports_dots
        from opi.core.config import settings

        logger = logging.getLogger(__name__)
        cluster = self.cluster or settings.CLUSTER_MANAGER
        supports_dots = False

        logger.debug(f"DomainFormatOptionsProvider.get_options(): base_domain={self.base_domain!r}, cluster={cluster}")

        if self.base_domain == "__custom__":
            supports_dots = True
            logger.debug("Custom domain selected, supports_dots=True")
        elif self.base_domain and cluster:
            supports_dots = get_domain_supports_dots(cluster, self.base_domain)
            logger.debug(f"Domain {self.base_domain!r} supports_dots={supports_dots}")
        else:
            logger.debug(f"base_domain={self.base_domain!r}, cluster={cluster}, no supports_dots check")

        format_ids = list(self._DASH_FORMATS)
        if supports_dots:
            format_ids.extend(self._DOT_FORMATS)

        format_ids.sort()
        logger.debug(f"DomainFormatOptionsProvider returning {len(format_ids)} formats: {format_ids}")
        return [{"value": f, "label": f"{f}.domein"} for f in format_ids]


class DeploymentSelectOptionsProvider:
    """Provides deployment names as checkbox options.

    Used when adding a component to select which deployments should
    receive a reference to the new component.
    """

    def __init__(self, deployment_names: list[str] | None = None) -> None:
        self.deployment_names = deployment_names or []

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": name, "label": name} for name in self.deployment_names]


class ApprovalStatusOptionsProvider:
    """Provides status options for the admin domain/subdomain approval flow."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "skip", "label": "Overslaan"},
            {"value": "approved", "label": "Goedkeuren"},
            {"value": "denied", "label": "Afwijzen"},
        ]


# Registry of all available providers
class AttachmentOptionsProvider:
    """Provides the ids of attachments in the project-level attachments catalog as options."""

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        self._yaml_data = yaml_data or {}

    def get_options(self) -> list[dict[str, Any]]:
        from opi.handlers.project_file_handler import extract_attachment_catalog

        catalog = extract_attachment_catalog(self._yaml_data)
        if not catalog:
            return [{"value": "", "label": "Geen bijlagen geüpload — upload eerst op de Bijlagen-sectie"}]
        return [{"value": entry["id"], "label": entry.get("filename", entry["id"])} for entry in catalog.values()]


class AttachmentProvideAsOptionsProvider:
    """Static options for how an attachment is delivered into the pod."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "file", "label": "Als bestand (gemount op een pad)"},
            {"value": "env-var", "label": "Als waarde van een env-var (alleen tekst)"},
        ]


PROVIDER_REGISTRY: dict[str, type[OptionsProvider]] = {
    "ClusterOptionsProvider": ClusterOptionsProvider,
    "ServiceOptionsProvider": ServiceOptionsProvider,
    "ComponentTypeOptionsProvider": ComponentTypeOptionsProvider,
    "UserRoleOptionsProvider": UserRoleOptionsProvider,
    "CpuRequestOptionsProvider": CpuRequestOptionsProvider,
    "CpuLimitOptionsProvider": CpuLimitOptionsProvider,
    "MemoryOptionsProvider": MemoryOptionsProvider,
    "MemoryRequestOptionsProvider": MemoryRequestOptionsProvider,
    "DomainModeOptionsProvider": DomainModeOptionsProvider,
    "StorageTypeOptionsProvider": StorageTypeOptionsProvider,
    "StorageSizeOptionsProvider": StorageSizeOptionsProvider,
    "KeycloakTemplateOptionsProvider": KeycloakTemplateOptionsProvider,
    "PullPolicyOptionsProvider": PullPolicyOptionsProvider,
    "BaseDomainOptionsProvider": BaseDomainOptionsProvider,
    "ClusterBaseDomainOptionsProvider": ClusterBaseDomainOptionsProvider,
    "FilteredServiceOptionsProvider": FilteredServiceOptionsProvider,
    "ComponentReferenceOptionsProvider": ComponentReferenceOptionsProvider,
    "BackupScheduleFrequencyOptionsProvider": BackupScheduleFrequencyOptionsProvider,
    "BackupScheduleTimeOptionsProvider": BackupScheduleTimeOptionsProvider,
    "BackupScheduleDayOptionsProvider": BackupScheduleDayOptionsProvider,
    "BackupScheduleMonthDayOptionsProvider": BackupScheduleMonthDayOptionsProvider,
    "BackupResourceTypesOptionsProvider": BackupResourceTypesOptionsProvider,
    "BackupDeploymentOptionsProvider": BackupDeploymentOptionsProvider,
    "DeploymentCloneFromOptionsProvider": DeploymentCloneFromOptionsProvider,
    "RootComponentOptionsProvider": RootComponentOptionsProvider,
    "BareDomainComponentOptionsProvider": BareDomainComponentOptionsProvider,
    "RepositoryOptionsProvider": RepositoryOptionsProvider,
    "DomainFormatOptionsProvider": DomainFormatOptionsProvider,
    "DeploymentSelectOptionsProvider": DeploymentSelectOptionsProvider,
    "ApprovalStatusOptionsProvider": ApprovalStatusOptionsProvider,
    "AttachmentOptionsProvider": AttachmentOptionsProvider,
    "AttachmentProvideAsOptionsProvider": AttachmentProvideAsOptionsProvider,
}


def get_provider(name: str, **kwargs: str | bool | int) -> OptionsProvider:
    """
    Get a provider instance by name.

    Args:
        name: Provider class name
        **kwargs: Arguments to pass to provider constructor

    Returns:
        Provider instance

    Raises:
        KeyError: If provider not found in registry
    """
    if name not in PROVIDER_REGISTRY:
        raise KeyError(f"Unknown provider: {name}")
    return PROVIDER_REGISTRY[name](**kwargs)


def get_all_providers() -> dict[str, OptionsProvider]:
    """
    Get instances of all registered providers.

    Returns:
        Dict mapping provider names to instances
    """
    return {name: cls() for name, cls in PROVIDER_REGISTRY.items()}
