"""
Tests for nice URL naming functionality.

Tests the nice URL dot-separated pattern for hostnames:
- component.subdomain.base_domain (nice-url mode)
- subdomain.base_domain (root URL)
"""

import pytest
from opi.utils.naming import (
    generate_nice_url_hostname,
    generate_nice_url_root_hostname,
    find_root_component,
    get_component_ingress_map,
    get_deployment_hostnames,
)
from opi.core.cluster_config import (
    get_nice_url_config,
    get_nice_url_supported_domains,
    is_nice_url_domain_supported,
)


class TestGenerateNiceUrlHostname:
    """Tests for generate_nice_url_hostname function."""

    def test_basic_pattern(self):
        """Basic nice URL pattern: component.subdomain.base_domain."""
        result = generate_nice_url_hostname("frontend", "myapp", "rijks.app")
        assert result == "frontend.myapp.rijks.app"

    def test_different_components(self):
        """Different component names work correctly."""
        assert generate_nice_url_hostname("backend", "myapp", "rijksapps.nl") == "backend.myapp.rijksapps.nl"
        assert generate_nice_url_hostname("api", "testdomain", "kind") == "api.testdomain.kind"

    def test_sanitizes_component_name(self):
        """Component names are lowercased."""
        result = generate_nice_url_hostname("My_Frontend", "myapp", "rijks.app")
        assert result == "my_frontend.myapp.rijks.app"

    def test_sanitizes_subdomain(self):
        """Subdomains are lowercased."""
        result = generate_nice_url_hostname("frontend", "MyApp", "rijks.app")
        assert result == "frontend.myapp.rijks.app"


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

    def test_finds_root_component_by_reference(self):
        """Finds component marked with root: true using reference field."""
        components = [
            {"reference": "frontend", "root": True},
            {"reference": "backend"},
        ]
        result = find_root_component(components)
        assert result == "frontend"

    def test_finds_root_component_by_name(self):
        """Finds component marked with root: true using name field."""
        components = [
            {"name": "api"},
            {"name": "web", "root": True},
        ]
        result = find_root_component(components)
        assert result == "web"

    def test_returns_none_when_no_root(self):
        """Returns None when no component has root: true."""
        components = [
            {"name": "api"},
            {"name": "web"},
        ]
        result = find_root_component(components)
        assert result is None

    def test_returns_none_for_empty_list(self):
        """Returns None for empty component list."""
        result = find_root_component([])
        assert result is None

    def test_prefers_reference_over_name(self):
        """Prefers 'reference' field over 'name' field."""
        components = [
            {"reference": "ref-name", "name": "fallback-name", "root": True},
        ]
        result = find_root_component(components)
        assert result == "ref-name"


class TestGetComponentIngressMapNiceUrl:
    """Tests for get_component_ingress_map with nice-url mode."""

    def test_nice_url_mode_generates_correct_hostname(self):
        """Nice URL mode generates dot-separated hostname."""
        result = get_component_ingress_map(
            component_name="frontend",
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="mydomain",
            base_domain="rijks.app",
            domain_mode="nice-url"
        )
        assert "prod-frontend" in result
        assert result["prod-frontend"] == "frontend.mydomain.rijks.app"

    def test_nice_url_mode_different_subdomain(self):
        """Nice URL mode uses subdomain correctly."""
        result = get_component_ingress_map(
            component_name="backend",
            deployment_name="staging",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="testapp",
            base_domain="rijks.app",
            domain_mode="nice-url"
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
            domain_mode=None  # Default mode
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
            base_domain="custom.nl"
        )
        assert "prod-frontend" in result
        assert result["prod-frontend"] == "myapp.custom.nl"


class TestGetDeploymentHostnamesNiceUrl:
    """Tests for get_deployment_hostnames with nice-url mode."""

    def test_nice_url_mode_multiple_components(self):
        """Nice URL mode generates unique hostnames for each component plus root."""
        result = get_deployment_hostnames(
            component_names=["frontend", "backend", "api"],
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="mydomain",
            base_domain="rijks.app",
            domain_mode="nice-url"
        )
        # Should have 4 hostnames: 3 components + 1 root
        assert len(result) == 4
        assert "frontend.mydomain.rijks.app" in result
        assert "backend.mydomain.rijks.app" in result
        assert "api.mydomain.rijks.app" in result
        assert "mydomain.rijks.app" in result  # Root hostname

    def test_nice_url_mode_includes_root_hostname(self):
        """Nice URL mode includes root hostname."""
        result = get_deployment_hostnames(
            component_names=["frontend"],
            deployment_name="staging",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="testapp",
            base_domain="rijks.app",
            domain_mode="nice-url"
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
        """Production cluster supports rijks.app and rijksapps.nl domains."""
        domains = get_nice_url_supported_domains("odcn-production")
        assert "rijks.app" in domains
        assert "rijksapps.nl" in domains

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
        with pytest.raises(ValueError):
            get_nice_url_supported_domains("nonexistent-cluster")
