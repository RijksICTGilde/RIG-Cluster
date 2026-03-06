"""Tests for the new options providers added in Sub-part C."""

from opi.forms.visualizers.providers import (
    BaseDomainOptionsProvider,
    ClusterBaseDomainOptionsProvider,
    ComponentReferenceOptionsProvider,
    DomainModeOptionsProvider,
    FilteredServiceOptionsProvider,
    KeycloakTemplateOptionsProvider,
    PullPolicyOptionsProvider,
    RepositoryOptionsProvider,
    StorageSizeOptionsProvider,
    StorageTypeOptionsProvider,
)


class TestStorageTypeOptionsProvider:
    def test_returns_options(self):
        options = StorageTypeOptionsProvider().get_options()
        assert len(options) == 2
        values = [o["value"] for o in options]
        assert "persistent" in values
        assert "ephemeral" in values


class TestStorageSizeOptionsProvider:
    def test_returns_options(self):
        provider = StorageSizeOptionsProvider()
        options = provider.get_options()
        assert len(options) > 0
        assert all("value" in o and "label" in o for o in options)

    def test_includes_common_sizes(self):
        options = StorageSizeOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert "250Mi" in values
        assert "1Gi" in values


class TestKeycloakTemplateOptionsProvider:
    def test_returns_options(self):
        provider = KeycloakTemplateOptionsProvider()
        options = provider.get_options()
        assert len(options) == 2
        assert all("value" in o and "label" in o for o in options)

    def test_includes_expected_template_values(self):
        options = KeycloakTemplateOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert "sso-support" in values
        assert "sso-only" in values


class TestPullPolicyOptionsProvider:
    def test_returns_three_policies(self):
        options = PullPolicyOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert values == ["Always", "IfNotPresent", "Never"]


class TestBaseDomainOptionsProvider:
    def test_returns_options(self):
        options = BaseDomainOptionsProvider().get_options()
        assert len(options) == 3
        values = [o["value"] for o in options]
        assert "" in values
        assert "rijksapp.nl" in values
        assert "__custom__" in values


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


class TestDomainModeOptionsProvider:
    def test_returns_four_options(self):
        options = DomainModeOptionsProvider().get_options()
        assert len(options) == 4

    def test_includes_nice_url(self):
        options = DomainModeOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert "nice-url" in values
        assert "component-specific" in values
        assert "deployment-name" in values
        assert "custom" in values


class TestClusterBaseDomainOptionsProvider:
    def test_returns_options_without_cluster(self):
        provider = ClusterBaseDomainOptionsProvider()
        options = provider.get_options()
        # Should aggregate domains from all clusters
        assert isinstance(options, list)

    def test_returns_options_with_unknown_cluster(self):
        provider = ClusterBaseDomainOptionsProvider(cluster="nonexistent")
        options = provider.get_options()
        # Falls back to all domains
        assert isinstance(options, list)

    def test_options_have_value_and_label(self):
        provider = ClusterBaseDomainOptionsProvider()
        options = provider.get_options()
        for opt in options:
            assert "value" in opt
            assert "label" in opt


class TestProviderRegistry:
    def test_new_providers_registered(self):
        """All providers are in the registry."""
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        new_names = [
            "StorageTypeOptionsProvider",
            "StorageSizeOptionsProvider",
            "KeycloakTemplateOptionsProvider",
            "PullPolicyOptionsProvider",
            "BaseDomainOptionsProvider",
            "FilteredServiceOptionsProvider",
            "ComponentReferenceOptionsProvider",
            "RepositoryOptionsProvider",
            "ClusterBaseDomainOptionsProvider",
        ]
        for name in new_names:
            assert name in PROVIDER_REGISTRY

    def test_get_provider_works_for_new_providers(self):
        from opi.forms.visualizers.providers import get_provider

        provider = get_provider("StorageSizeOptionsProvider")
        assert len(provider.get_options()) > 0


class TestResolveOptionsWithMixedContext:
    """Verify that resolve_options_for_editable filters context kwargs correctly."""

    def test_filtered_provider_receives_project_services_despite_extra_keys(self):
        """FilteredServiceOptionsProvider must receive project_services even when
        the context contains keys meant for other providers (e.g. component_names).
        This was a bug: the extra keys caused a TypeError, and the fallback
        re-instantiated the provider without any kwargs at all."""
        from opi.forms.visualizers.bridge import resolve_options_for_editable
        from opi.forms.visualizers.fields.components import COMPONENT_SERVICES

        context = {
            "project_services": ["publish-on-web", "keycloak"],
            "component_names": ["frontend"],
        }
        options = resolve_options_for_editable(COMPONENT_SERVICES, context=context)
        values = [o["value"] for o in options]
        assert "publish-on-web" in values
        assert "keycloak" in values

    def test_provider_without_context_params_still_works(self):
        """Providers that accept no kwargs should still work when context is passed."""
        from opi.forms.editables.editable import Editable, WidgetType
        from opi.forms.visualizers.bridge import resolve_options_for_editable
        from opi.forms.visualizers.visualizer import EditableVisualizer

        editable = EditableVisualizer(
            editable=Editable(yaml_path="test", values_provider="CpuRequestOptionsProvider"),
            widget=WidgetType.SELECT,
            label="Test",
        )
        context = {"project_services": ["keycloak"], "component_names": ["api"]}
        options = resolve_options_for_editable(editable, context=context)
        assert len(options) > 0
