"""
Tests for transient_value_when_none on Editable.

Tests that transient values are resolved on demand via get_effective_value,
visible to dependent fields during evaluation, and never mutate yaml_data.
"""

import pytest
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.resolvers import build_resolver_map, get_effective_value
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path, get_domain_setting


class _StaticResolver:
    """Test resolver that returns a fixed value."""

    def __init__(self, value: str) -> None:
        self._value = value

    def resolve(self, yaml_data: dict) -> str:
        return self._value


class TestBuildResolverMap:
    def test_collects_resolvers_from_editables(self):
        ed = Editable(
            yaml_path="base-domain",
            transient_value_when_none=_StaticResolver("default.example.com"),
        )
        vis = EditableVisualizer(editable=ed, widget=WidgetType.TEXT, label="Domain")

        resolvers = build_resolver_map([vis])
        assert "base-domain" in resolvers

    def test_ignores_editables_without_resolver(self):
        ed = Editable(yaml_path="name")
        vis = EditableVisualizer(editable=ed, widget=WidgetType.TEXT, label="Name")

        resolvers = build_resolver_map([vis])
        assert len(resolvers) == 0

    def test_recurses_into_children(self):
        child_ed = Editable(
            yaml_path="deployments[0]/base-domain",
            transient_value_when_none=_StaticResolver("child.example.com"),
        )
        child_vis = EditableVisualizer(editable=child_ed, widget=WidgetType.SELECT, label="Domain")

        parent_ed = Editable(yaml_path="deployments[0]")
        parent_vis = EditableVisualizer(
            editable=parent_ed, widget=WidgetType.GROUP, label="Group", children=[child_vis]
        )

        resolvers = build_resolver_map([parent_vis])
        assert "deployments[0]/base-domain" in resolvers


class TestGetEffectiveValue:
    def test_returns_existing_value(self):
        yaml_data = {"name": "test"}
        assert get_effective_value(yaml_data, "name") == "test"

    def test_returns_none_without_resolver(self):
        yaml_data: dict = {}
        assert get_effective_value(yaml_data, "name") is None

    def test_resolves_when_none_with_resolver(self):
        yaml_data: dict = {}
        resolvers = {"base-domain": _StaticResolver("default.example.com")}

        result = get_effective_value(yaml_data, "base-domain", resolvers)
        assert result == "default.example.com"

    def test_does_not_resolve_when_value_exists(self):
        yaml_data = {"base-domain": "custom.example.com"}
        resolvers = {"base-domain": _StaticResolver("default.example.com")}

        result = get_effective_value(yaml_data, "base-domain", resolvers)
        assert result == "custom.example.com"

    def test_does_not_mutate_yaml_data(self):
        yaml_data: dict = {}
        resolvers = {"base-domain": _StaticResolver("default.example.com")}

        get_effective_value(yaml_data, "base-domain", resolvers)
        assert "base-domain" not in yaml_data


class TestClusterDefaultDomain:
    def test_returns_cluster_default_ingress_domain(self, monkeypatch):
        from opi.forms.editables.resolvers import ClusterDefaultDomain

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())

        resolver = ClusterDefaultDomain()
        result = resolver.resolve({})

        # The cluster default is the ingress domain, not the first nice-URL domain.
        assert result == "sandbox.rijksapp.dev"

    def test_raises_for_unknown_cluster(self, monkeypatch):
        from opi.forms.editables.resolvers import ClusterDefaultDomain

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "test"})())

        # A misconfigured CLUSTER_MANAGER must surface, not silently resolve to None.
        with pytest.raises(ValueError, match="test"):
            ClusterDefaultDomain().resolve({})


class TestConditionWithResolvers:
    """Test that SubdomainNeedsRequestCondition uses resolvers for base-domain."""

    def test_condition_resolves_default_domain(self, monkeypatch):
        from opi.forms.editables.conditions import SubdomainNeedsRequestCondition

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())

        condition = SubdomainNeedsRequestCondition()

        # Without resolvers: base-domain is None, condition returns False
        yaml_data = {
            "deployments": [{"subdomain": "test", "domain-format": "subdomain"}],
        }
        assert condition.check(yaml_data) is False

        # With resolvers: base-domain resolved from cluster config
        resolvers = build_resolver_map(
            [
                EditableVisualizer(
                    editable=Editable(
                        yaml_path=domain_setting_path(DomainSetting.BASE_DOMAIN, 0),
                        transient_value_when_none=_StaticResolver("sandbox.rijksapp.dev"),
                    ),
                    widget=WidgetType.SELECT,
                    label="Domain",
                ),
            ]
        )
        condition.set_resolvers(resolvers)
        assert condition.check(yaml_data) is True

        # yaml_data was NOT mutated
        assert get_domain_setting(yaml_data["deployments"][0], DomainSetting.BASE_DOMAIN) is None
