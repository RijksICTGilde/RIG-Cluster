"""
Tests for nice URL naming functionality.

Tests the nice URL dot-separated pattern for hostnames:
- component.subdomain.base_domain (domain-format component.subdomain)
- subdomain.base_domain (root URL, when a root-component is set)
"""

import pytest
from opi.core.cluster_config import (
    get_nice_url_config,
    get_nice_url_supported_domains,
    is_nice_url_domain_supported,
)
from opi.utils.naming import (
    find_root_component,
    generate_nice_url_root_hostname,
    get_component_ingress_map,
    get_deployment_hostnames,
)

_CLUSTER = "local"


def _approved(*domains: str) -> dict:
    """Een project dat deze domeinen goedgekeurd heeft.

    Sinds de goedkeuringspoort voor ELKE vorm geldt (en niet alleen voor een
    deployment die een ``domain-format`` noemt) levert een leeg project hier het
    veilige clusteradres. Deze tests gaan over het samenstellen van de nette URL, dus
    hoort het domein goedgekeurd te zijn; de terugval zelf staat in
    ``tests/test_domain_approval.py``.
    """
    return {"domains": {"allowed-domains": [{"domain": d, "status": "approved"} for d in domains]}}


class TestGenerateNiceUrlRootHostname:
    """Tests for generate_nice_url_root_hostname function."""

    def test_basic_root_pattern(self):
        """Basic root URL pattern: subdomain.base_domain."""
        result = generate_nice_url_root_hostname("myapp", "rijks.app")
        assert result == "myapp.rijks.app"

    def test_different_domains(self):
        """Different base domains work correctly."""
        assert generate_nice_url_root_hostname("testapp", "rijksapps.nl") == "testapp.rijksapps.nl"
        assert generate_nice_url_root_hostname("local", "kind") == "local.kind"

    def test_sanitizes_subdomain(self):
        """Subdomains are lowercased."""
        result = generate_nice_url_root_hostname("MyApp", "rijks.app")
        assert result == "myapp.rijks.app"


class TestFindRootComponent:
    """Tests for find_root_component function."""

    def test_finds_root_component_from_deployment(self):
        """Reads root-component from deployment dict."""
        deployment = {"name": "prod", "root-component": "frontend"}
        assert find_root_component(deployment) == "frontend"

    def test_returns_none_when_no_root(self):
        """Returns None when no root-component set."""
        deployment = {"name": "prod"}
        assert find_root_component(deployment) is None

    def test_returns_none_for_empty_dict(self):
        """Returns None for empty deployment dict."""
        assert find_root_component({}) is None


class TestGetComponentIngressMapNiceUrl:
    """Tests for get_component_ingress_map with the component.subdomain format."""

    def test_nice_url_format_generates_correct_hostname(self):
        """The component.subdomain format generates a dot-separated hostname."""
        result = get_component_ingress_map(
            component_name="frontend",
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="mydomain",
            base_domain="rijks.app",
            domain_format="component.subdomain",
            project_data=_approved("rijks.app"),
            cluster=_CLUSTER,
        )
        assert "prod-frontend" in result
        assert result["prod-frontend"] == "frontend.mydomain.rijks.app"

    def test_nice_url_format_different_subdomain(self):
        """The component.subdomain format uses the subdomain correctly."""
        result = get_component_ingress_map(
            component_name="backend",
            deployment_name="staging",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="testapp",
            base_domain="rijks.app",
            domain_format="component.subdomain",
            project_data=_approved("rijks.app"),
            cluster=_CLUSTER,
        )
        assert "staging-backend" in result
        assert result["staging-backend"] == "backend.testapp.rijks.app"

    def test_backward_compatibility_component_specific(self):
        """Backward compatibility: component-specific mode still works."""
        result = get_component_ingress_map(
            component_name="frontend",
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".cluster.example.com",
            project_data={},
            cluster=_CLUSTER,
        )
        # Should use the default generate_ingress_map behavior
        assert "prod-frontend" in result

    def test_backward_compatibility_custom_domain(self):
        """Backward compatibility: custom domain with subdomain still works."""
        result = get_component_ingress_map(
            component_name="frontend",
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".cluster.example.com",
            subdomain="myapp",
            base_domain="custom.nl",
            project_data=_approved("custom.nl"),
            cluster=_CLUSTER,
        )
        assert "prod-frontend" in result
        assert result["prod-frontend"] == "myapp.custom.nl"


class TestGetDeploymentHostnamesNiceUrl:
    """Tests for get_deployment_hostnames with the component.subdomain format."""

    def test_nice_url_format_multiple_components(self):
        """The format generates unique hostnames for each component plus root."""
        result = get_deployment_hostnames(
            component_names=["frontend", "backend", "api"],
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="mydomain",
            base_domain="rijks.app",
            domain_format="component.subdomain",
            root_component="frontend",
            project_data=_approved("rijks.app"),
            cluster=_CLUSTER,
        )
        # Should have 4 hostnames: 3 components + 1 root
        assert len(result) == 4
        assert "frontend.mydomain.rijks.app" in result
        assert "backend.mydomain.rijks.app" in result
        assert "api.mydomain.rijks.app" in result
        assert "mydomain.rijks.app" in result  # Root hostname

    def test_nice_url_format_includes_root_hostname(self):
        """The format includes the root hostname when a root component is set."""
        result = get_deployment_hostnames(
            component_names=["frontend"],
            deployment_name="staging",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="testapp",
            base_domain="rijks.app",
            domain_format="component.subdomain",
            root_component="frontend",
            project_data=_approved("rijks.app"),
            cluster=_CLUSTER,
        )
        assert len(result) == 2
        assert "frontend.testapp.rijks.app" in result
        assert "testapp.rijks.app" in result  # Root hostname


class TestClusterConfigNiceUrl:
    """Tests for cluster configuration nice URL functions."""

    def test_get_nice_url_config_local(self):
        """Local cluster has nice URL config."""
        config = get_nice_url_config("local")
        assert config is not None
        assert "supported_domains" in config

    def test_get_nice_url_config_production(self):
        """Production cluster has nice URL config."""
        config = get_nice_url_config("odcn-production")
        assert config is not None
        assert "supported_domains" in config

    def test_get_nice_url_supported_domains_local(self):
        """Local cluster supports kind and local domains."""
        domains = get_nice_url_supported_domains("local")
        assert "kind" in domains
        assert "local" in domains

    def test_get_nice_url_supported_domains_production(self):
        """Production cluster supports the app base domains (not the cluster infra domain rijksapps.nl)."""
        domains = get_nice_url_supported_domains("odcn-production")
        assert "rijks.app" in domains
        assert "rijksapp.dev" in domains
        assert "rijksapps.nl" not in domains

    def test_is_nice_url_domain_supported_true(self):
        """Check if rijks.app is supported on production."""
        assert is_nice_url_domain_supported("odcn-production", "rijks.app") is True

    def test_is_nice_url_domain_supported_false(self):
        """Check if unsupported domain returns False."""
        assert is_nice_url_domain_supported("odcn-production", "example.com") is False

    def test_is_nice_url_domain_supported_local(self):
        """Check if kind is supported on local."""
        assert is_nice_url_domain_supported("local", "kind") is True

    def test_unknown_cluster_raises_error(self):
        """Unknown cluster should raise ValueError."""
        with pytest.raises(ValueError, match="not found in configuration"):
            get_nice_url_supported_domains("nonexistent-cluster")
