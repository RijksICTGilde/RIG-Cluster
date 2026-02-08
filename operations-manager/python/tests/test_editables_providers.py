"""Tests for the new options providers added in Sub-part C."""

from opi.forms.providers import (
    BaseDomainOptionsProvider,
    ComponentReferenceOptionsProvider,
    FilteredServiceOptionsProvider,
    KeycloakTemplateOptionsProvider,
    PullPolicyOptionsProvider,
    RepositoryOptionsProvider,
    StorageSizeOptionsProvider,
)


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


class TestKeycloakTemplateOptionsProvider:
    def test_returns_options(self):
        provider = KeycloakTemplateOptionsProvider()
        options = provider.get_options()
        assert len(options) == 2
        assert all("value" in o and "label" in o for o in options)

    def test_includes_descriptions(self):
        options = KeycloakTemplateOptionsProvider().get_options()
        assert all("description" in o for o in options)


class TestPullPolicyOptionsProvider:
    def test_returns_three_policies(self):
        options = PullPolicyOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert values == ["Always", "IfNotPresent", "Never"]


class TestBaseDomainOptionsProvider:
    def test_returns_options(self):
        options = BaseDomainOptionsProvider().get_options()
        assert len(options) == 2
        values = [o["value"] for o in options]
        assert "" in values
        assert "rijksapp.nl" in values


class TestFilteredServiceOptionsProvider:
    def test_empty_project_services(self):
        provider = FilteredServiceOptionsProvider(project_services=[])
        assert provider.get_options() == []

    def test_filters_to_project_services(self):
        provider = FilteredServiceOptionsProvider(project_services=["publish-on-web", "keycloak"])
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
        provider = ComponentReferenceOptionsProvider(component_names=["web", "api", "worker"])
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

    def test_empty_names(self):
        provider = RepositoryOptionsProvider()
        assert provider.get_options() == []


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
