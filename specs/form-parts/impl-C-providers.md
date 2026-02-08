# Sub-part C: New Providers

**Layer:** 0 (no dependencies)
**Files to modify:**
- `opi/forms/providers.py` (add 7 new provider classes + registry entries)

**Files to create:**
- `tests/test_editables_providers.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

Add 7 new options providers to the existing `PROVIDER_REGISTRY` in `opi/forms/providers.py`. These providers supply dynamic options for select fields, checkbox groups, and other choice-based widgets in the editable-driven forms.

**IMPORTANT:** Do not modify or break existing providers. Only add new classes and registry entries.

## Existing providers (reference)

The file already contains: `ClusterOptionsProvider`, `ServiceOptionsProvider`, `ComponentTypeOptionsProvider`, `UserRoleOptionsProvider`, `CpuLimitOptionsProvider`, `MemoryLimitOptionsProvider`, `DomainModeOptionsProvider`.

All providers implement the `OptionsProvider` protocol:
```python
class OptionsProvider(Protocol):
    def get_options(self) -> list[dict[str, Any]]: ...
```

Each option dict has at minimum `value` and `label` keys. May also include `description`, `icon`, `color`, `scope`.

## Providers to Add

### StorageSizeOptionsProvider

```python
class StorageSizeOptionsProvider:
    """Provides storage size options for persistent volumes."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "1Gi", "label": "1 GB"},
            {"value": "5Gi", "label": "5 GB"},
            {"value": "10Gi", "label": "10 GB"},
            {"value": "20Gi", "label": "20 GB"},
            {"value": "50Gi", "label": "50 GB"},
            {"value": "100Gi", "label": "100 GB"},
        ]
```

### KeycloakTemplateOptionsProvider

```python
class KeycloakTemplateOptionsProvider:
    """Provides Keycloak realm template options."""

    def get_options(self) -> list[dict[str, Any]]:
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
```

### PullPolicyOptionsProvider

```python
class PullPolicyOptionsProvider:
    """Provides Kubernetes image pull policy options."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "Always", "label": "Always"},
            {"value": "IfNotPresent", "label": "IfNotPresent"},
            {"value": "Never", "label": "Never"},
        ]
```

### BaseDomainOptionsProvider

```python
class BaseDomainOptionsProvider:
    """Provides base domain options for deployment URLs."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "", "label": "Standaard (clusternaam)"},
            {"value": "rijksapp.nl", "label": "rijksapp.nl"},
        ]
```

### FilteredServiceOptionsProvider

This is the most complex one — it filters the full service list to only those enabled at the project level.

```python
class FilteredServiceOptionsProvider:
    """
    Provides service options filtered to project-level enabled services.

    Used by component `uses-services` checkbox group. Only shows services
    that the project has enabled (cross-part dependency).
    """

    def __init__(self, project_services: list[str] | None = None) -> None:
        self.project_services = project_services or []

    def get_options(self) -> list[dict[str, Any]]:
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
```

### ComponentReferenceOptionsProvider

```python
class ComponentReferenceOptionsProvider:
    """
    Provides component names from the project as select options.

    Used by deployment component reference selects (cross-part dependency).
    """

    def __init__(self, component_names: list[str] | None = None) -> None:
        self.component_names = component_names or []

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": name, "label": name} for name in self.component_names]
```

### RepositoryOptionsProvider

```python
class RepositoryOptionsProvider:
    """
    Provides repository names from the project as select options.

    Used by deployment repository selects (cross-part dependency).
    """

    def __init__(self, repository_names: list[str] | None = None) -> None:
        self.repository_names = repository_names or []

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": name, "label": name} for name in self.repository_names]
```

## PROVIDER_REGISTRY Updates

Add all 7 new classes to the existing `PROVIDER_REGISTRY` dict:

```python
PROVIDER_REGISTRY: dict[str, type[OptionsProvider]] = {
    # ... existing entries ...
    "StorageSizeOptionsProvider": StorageSizeOptionsProvider,
    "KeycloakTemplateOptionsProvider": KeycloakTemplateOptionsProvider,
    "PullPolicyOptionsProvider": PullPolicyOptionsProvider,
    "BaseDomainOptionsProvider": BaseDomainOptionsProvider,
    "FilteredServiceOptionsProvider": FilteredServiceOptionsProvider,
    "ComponentReferenceOptionsProvider": ComponentReferenceOptionsProvider,
    "RepositoryOptionsProvider": RepositoryOptionsProvider,
}
```

## Tests: test_editables_providers.py

```python
class TestStorageSizeOptionsProvider:
    def test_returns_options(self):
        provider = StorageSizeOptionsProvider()
        options = provider.get_options()
        assert len(options) > 0
        assert all("value" in o and "label" in o for o in options)

    def test_includes_common_sizes(self):
        options = StorageSizeOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert "1Gi" in values
        assert "10Gi" in values


class TestPullPolicyOptionsProvider:
    def test_returns_three_policies(self):
        options = PullPolicyOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert values == ["Always", "IfNotPresent", "Never"]


class TestFilteredServiceOptionsProvider:
    def test_empty_project_services(self):
        provider = FilteredServiceOptionsProvider(project_services=[])
        assert provider.get_options() == []

    def test_filters_to_project_services(self):
        provider = FilteredServiceOptionsProvider(
            project_services=["publish-on-web", "keycloak"]
        )
        options = provider.get_options()
        values = [o["value"] for o in options]
        assert "publish-on-web" in values
        assert "keycloak" in values
        # Should NOT include services not in project_services
        assert "redis" not in values

    def test_default_none_project_services(self):
        provider = FilteredServiceOptionsProvider()
        assert provider.get_options() == []


class TestComponentReferenceOptionsProvider:
    def test_returns_component_names(self):
        provider = ComponentReferenceOptionsProvider(
            component_names=["web", "api", "worker"]
        )
        options = provider.get_options()
        assert len(options) == 3
        assert options[0] == {"value": "web", "label": "web"}

    def test_empty_names(self):
        provider = ComponentReferenceOptionsProvider()
        assert provider.get_options() == []


class TestRepositoryOptionsProvider:
    def test_returns_repository_names(self):
        provider = RepositoryOptionsProvider(repository_names=["main-repo"])
        options = provider.get_options()
        assert len(options) == 1
        assert options[0] == {"value": "main-repo", "label": "main-repo"}


class TestProviderRegistry:
    def test_new_providers_registered(self):
        """All 7 new providers are in the registry."""
        from opi.forms.providers import PROVIDER_REGISTRY
        new_names = [
            "StorageSizeOptionsProvider",
            "KeycloakTemplateOptionsProvider",
            "PullPolicyOptionsProvider",
            "BaseDomainOptionsProvider",
            "FilteredServiceOptionsProvider",
            "ComponentReferenceOptionsProvider",
            "RepositoryOptionsProvider",
        ]
        for name in new_names:
            assert name in PROVIDER_REGISTRY

    def test_get_provider_works_for_new_providers(self):
        from opi.forms.providers import get_provider
        provider = get_provider("StorageSizeOptionsProvider")
        assert len(provider.get_options()) > 0
```

## Code Style

- Follow the existing provider patterns exactly (same docstring style, same return format)
- Use lowercase type hints: `dict`, `list`
- Use `|` for unions: `str | None`
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
