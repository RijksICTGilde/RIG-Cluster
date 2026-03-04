"""
Options providers for dynamic form field population.

This module provides the OptionsProvider protocol and concrete implementations
for populating select/radio fields with dynamic data from OPI's domain.
"""

from typing import Any, Protocol

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

            # Filter by scope if specified
            if self.filter_scope and definition.scope != self.filter_scope:
                continue

            options.append(
                {
                    "value": service_type.value,
                    "label": definition.name,
                    "description": definition.description,
                    "icon": definition.icon,
                    "color": definition.color,
                    "scope": definition.scope,
                }
            )

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


class CpuLimitOptionsProvider:
    """Provides CPU limit options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available CPU limit options."""
        return [
            {"value": "1", "label": "1 CPU"},
            {"value": "2", "label": "2 CPU"},
            {"value": "3", "label": "3 CPU"},
            {"value": "4", "label": "4 CPU"},
        ]


class MemoryLimitOptionsProvider:
    """Provides memory limit options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available memory limit options."""
        return [
            {"value": "128Mi", "label": "128 MB"},
            {"value": "256Mi", "label": "256 MB"},
            {"value": "512Mi", "label": "512 MB"},
            {"value": "768Mi", "label": "768 MB"},
            {"value": "1Gi", "label": "1 GB"},
            {"value": "2Gi", "label": "2 GB"},
        ]


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
        ]


class StorageSizeOptionsProvider:
    """Provides storage size options for persistent volumes."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available storage size options."""
        return [
            {"value": "1Gi", "label": "1 GB"},
            {"value": "5Gi", "label": "5 GB"},
            {"value": "10Gi", "label": "10 GB"},
            {"value": "20Gi", "label": "20 GB"},
            {"value": "50Gi", "label": "50 GB"},
            {"value": "100Gi", "label": "100 GB"},
        ]


class KeycloakTemplateOptionsProvider:
    """Provides Keycloak realm template options."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available Keycloak template options."""
        return [
            {
                "value": "sso-support",
                "label": "SSO Support (standaard)",
                "description": "Basis SSO configuratie voor applicaties",
            },
            {
                "value": "sso-support-with-users",
                "label": "SSO Support met gebruikersbeheer",
                "description": "SSO configuratie met lokaal gebruikersbeheer in Keycloak",
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
        ]


class FilteredServiceOptionsProvider:
    """
    Provides service options filtered to project-level enabled services.

    Used by component `uses-services` checkbox group. Only shows services
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

    def __init__(self, component_names: list[str] | None = None) -> None:
        self.component_names = component_names or []

    def get_options(self) -> list[dict[str, Any]]:
        """Get component name options."""
        return [{"value": name, "label": name} for name in self.component_names]


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


# Registry of all available providers
PROVIDER_REGISTRY: dict[str, type[OptionsProvider]] = {
    "ClusterOptionsProvider": ClusterOptionsProvider,
    "ServiceOptionsProvider": ServiceOptionsProvider,
    "ComponentTypeOptionsProvider": ComponentTypeOptionsProvider,
    "UserRoleOptionsProvider": UserRoleOptionsProvider,
    "CpuLimitOptionsProvider": CpuLimitOptionsProvider,
    "MemoryLimitOptionsProvider": MemoryLimitOptionsProvider,
    "DomainModeOptionsProvider": DomainModeOptionsProvider,
    "StorageSizeOptionsProvider": StorageSizeOptionsProvider,
    "KeycloakTemplateOptionsProvider": KeycloakTemplateOptionsProvider,
    "PullPolicyOptionsProvider": PullPolicyOptionsProvider,
    "BaseDomainOptionsProvider": BaseDomainOptionsProvider,
    "FilteredServiceOptionsProvider": FilteredServiceOptionsProvider,
    "ComponentReferenceOptionsProvider": ComponentReferenceOptionsProvider,
    "RepositoryOptionsProvider": RepositoryOptionsProvider,
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
