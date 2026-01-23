"""
Tests for nice URL naming functionality.

Tests the nice URL dot-separated pattern for hostnames:
- component.deployment.base_domain
- component.deployment.project.base_domain (with include_project_name)
"""

import pytest
from opi.utils.naming import (
    generate_nice_url_hostname,
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
        """Basic nice URL pattern: component.deployment.base_domain."""
        result = generate_nice_url_hostname("frontend", "prod", "rijks.app")
        assert result == "frontend.prod.rijks.app"

    def test_different_components(self):
        """Different component names work correctly."""
        assert generate_nice_url_hostname("backend", "staging", "rijksapps.nl") == "backend.staging.rijksapps.nl"
        assert generate_nice_url_hostname("api", "dev", "kind") == "api.dev.kind"

    def test_with_project_name_disabled(self):
        """Project name is not included when include_project_name is False."""
        result = generate_nice_url_hostname(
            "frontend", "prod", "rijks.app",
            project_name="myapp", include_project_name=False
        )
        assert result == "frontend.prod.rijks.app"

    def test_with_project_name_enabled(self):
        """Project name is included when include_project_name is True."""
        result = generate_nice_url_hostname(
            "frontend", "prod", "rijks.app",
            project_name="myapp", include_project_name=True
        )
        assert result == "frontend.prod.myapp.rijks.app"

    def test_sanitizes_component_name(self):
        """Component names are lowercased and kept as-is with underscores."""
        result = generate_nice_url_hostname("My_Frontend", "prod", "rijks.app")
        assert result == "my_frontend.prod.rijks.app"

    def test_sanitizes_deployment_name(self):
        """Deployment names are lowercased and kept as-is with underscores."""
        result = generate_nice_url_hostname("frontend", "Production_Env", "rijks.app")
        assert result == "frontend.production_env.rijks.app"

    def test_sanitizes_project_name(self):
        """Project names are lowercased when included."""
        result = generate_nice_url_hostname(
            "frontend", "prod", "rijks.app",
            project_name="My_App_123", include_project_name=True
        )
        assert result == "frontend.prod.my_app_123.rijks.app"

    def test_include_project_name_without_project_name_param(self):
        """When include_project_name is True but project_name is None, don't include it."""
        result = generate_nice_url_hostname(
            "frontend", "prod", "rijks.app",
            project_name=None, include_project_name=True
        )
        assert result == "frontend.prod.rijks.app"


class TestGetComponentIngressMapNiceUrl:
    """Tests for get_component_ingress_map with nice-url mode."""

    def test_nice_url_mode_generates_correct_hostname(self):
        """Nice URL mode generates dot-separated hostname."""
        result = get_component_ingress_map(
            component_name="frontend",
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            base_domain="rijks.app",
            domain_mode="nice-url"
        )
        assert "prod-frontend" in result
        assert result["prod-frontend"] == "frontend.prod.rijks.app"

    def test_nice_url_mode_with_project_name(self):
        """Nice URL mode includes project name when requested."""
        result = get_component_ingress_map(
            component_name="frontend",
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            base_domain="rijks.app",
            domain_mode="nice-url",
            include_project_name=True
        )
        assert "prod-frontend" in result
        assert result["prod-frontend"] == "frontend.prod.myapp.rijks.app"

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
        """Nice URL mode generates unique hostnames for each component."""
        result = get_deployment_hostnames(
            component_names=["frontend", "backend", "api"],
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            base_domain="rijks.app",
            domain_mode="nice-url"
        )
        assert len(result) == 3
        assert "frontend.prod.rijks.app" in result
        assert "backend.prod.rijks.app" in result
        assert "api.prod.rijks.app" in result

    def test_nice_url_mode_with_project_name(self):
        """Nice URL mode includes project name in all hostnames when requested."""
        result = get_deployment_hostnames(
            component_names=["frontend", "backend"],
            deployment_name="staging",
            project_name="myapp",
            ingress_postfix=".kind",
            base_domain="rijks.app",
            domain_mode="nice-url",
            include_project_name=True
        )
        assert len(result) == 2
        assert "frontend.staging.myapp.rijks.app" in result
        assert "backend.staging.myapp.rijks.app" in result


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
