"""
Tests for the domain-format feature.

Tests the configurable hostname template system:
- Template resolution (dash and dot variants)
- resolve_domain_tail helper
- get_component_ingress_map with domain_format parameter
- get_deployment_hostnames with domain_format parameter
- Backward compatibility (domain_format=None)
- DomainFormatOptionsProvider filtering
- DomainFormatValidator
"""

import pytest
from opi.forms.editables.validators import DomainFormatValidator
from opi.forms.visualizers.providers import DomainFormatOptionsProvider
from opi.utils.naming import (
    DOMAIN_FORMAT_TEMPLATES,
    DOMAIN_MODE_DEFAULT_FORMAT,
    HostnameFormat,
    generate_hostname_from_format,
    get_component_ingress_map,
    get_deployment_hostnames,
    resolve_domain_tail,
)

# ---------------------------------------------------------------------------
# resolve_domain_tail
# ---------------------------------------------------------------------------


class TestResolveDomainTail:
    def test_returns_base_domain_when_set(self):
        assert resolve_domain_tail("rijksapp.dev", ".kind") == "rijksapp.dev"

    def test_returns_ingress_postfix_without_leading_dot(self):
        assert resolve_domain_tail(None, ".kind") == "kind"

    def test_returns_ingress_postfix_already_clean(self):
        assert resolve_domain_tail(None, "sandbox.rijksapp.dev") == "sandbox.rijksapp.dev"

    def test_empty_base_domain_uses_postfix(self):
        assert resolve_domain_tail("", ".cluster.local") == "cluster.local"


# ---------------------------------------------------------------------------
# generate_hostname_from_format
# ---------------------------------------------------------------------------


class TestGenerateHostnameFromFormat:
    """Test each template ID produces expected hostnames in both variants."""

    def test_component_deployment_project_dashes(self):
        result = generate_hostname_from_format("component-deployment-project", "frontend", "poc", "myapp", None, "kind")
        assert result == "frontend-poc-myapp.kind"

    def test_component_deployment_project_dots(self):
        result = generate_hostname_from_format(
            "component-deployment-project", "frontend", "poc", "myapp", None, "rijksapp.dev", use_dots=True
        )
        assert result == "frontend.poc.myapp.rijksapp.dev"

    def test_component_deployment_subdomain_dashes(self):
        result = generate_hostname_from_format(
            "component-deployment-subdomain", "frontend", "poc", "myapp", "moza", "kind"
        )
        assert result == "frontend-poc-moza.kind"

    def test_component_deployment_subdomain_dots(self):
        result = generate_hostname_from_format(
            "component-deployment-subdomain", "frontend", "poc", "myapp", "moza", "rijksapp.dev", use_dots=True
        )
        assert result == "frontend.poc.moza.rijksapp.dev"

    def test_deployment_project_dashes(self):
        result = generate_hostname_from_format("deployment-project", "frontend", "poc", "myapp", None, "kind")
        assert result == "poc-myapp.kind"

    def test_deployment_project_dots(self):
        result = generate_hostname_from_format(
            "deployment-project", "frontend", "poc", "myapp", None, "rijksapp.dev", use_dots=True
        )
        assert result == "poc.myapp.rijksapp.dev"

    def test_deployment_subdomain_dashes(self):
        result = generate_hostname_from_format("deployment-subdomain", "frontend", "poc", "myapp", "moza", "kind")
        assert result == "poc-moza.kind"

    def test_deployment_subdomain_dots(self):
        result = generate_hostname_from_format(
            "deployment-subdomain", "frontend", "poc", "myapp", "moza", "rijksapp.dev", use_dots=True
        )
        assert result == "poc.moza.rijksapp.dev"

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="Unknown domain-format"):
            generate_hostname_from_format("nonexistent", "fe", "poc", "app", None, "kind")

    def test_values_are_lowercased(self):
        result = generate_hostname_from_format("component-deployment-project", "Frontend", "POC", "MyApp", None, "kind")
        assert result == "frontend-poc-myapp.kind"


# ---------------------------------------------------------------------------
# get_component_ingress_map with domain_format
# ---------------------------------------------------------------------------


class TestGetComponentIngressMapWithDomainFormat:
    """Test that domain_format takes precedence over legacy dispatch."""

    def test_domain_format_dashes(self):
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            domain_format="component-deployment-project",
        )
        assert result == {"poc-frontend": "frontend-poc-myapp.kind"}

    def test_domain_format_dots(self):
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            subdomain="moza",
            base_domain="rijksapp.dev",
            hostname_format=HostnameFormat.DOTS,
            domain_format="deployment-subdomain",
        )
        assert result == {"poc-frontend": "poc.moza.rijksapp.dev"}

    def test_domain_format_uses_base_domain_as_domain(self):
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            base_domain="rijksapp.dev",
            hostname_format=HostnameFormat.DOTS,
            domain_format="deployment-project",
        )
        assert result == {"poc-frontend": "poc.myapp.rijksapp.dev"}

    def test_domain_format_falls_back_to_ingress_postfix(self):
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            domain_format="deployment-project",
        )
        assert result == {"poc-frontend": "poc-myapp.kind"}

    def test_none_domain_format_uses_legacy(self):
        """When domain_format is None, legacy dispatch is used (backward compat)."""
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            domain_format=None,
        )
        # Legacy: component-deployment-project.cluster
        assert result == {"poc-frontend": "frontend-poc-myapp.kind"}

    def test_none_domain_format_uses_legacy_dots(self):
        """When domain_format is None with DOTS, legacy nice-url is used."""
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            subdomain="moza",
            base_domain="rijksapp.dev",
            hostname_format=HostnameFormat.DOTS,
            domain_format=None,
        )
        # Legacy nice-url: component.subdomain.base_domain
        assert result == {"poc-frontend": "frontend.moza.rijksapp.dev"}


# ---------------------------------------------------------------------------
# get_deployment_hostnames with domain_format
# ---------------------------------------------------------------------------


class TestGetDeploymentHostnamesWithDomainFormat:
    def test_domain_format_no_root_hostname_added(self):
        """When domain_format is set, no automatic root hostname is appended."""
        hostnames = get_deployment_hostnames(
            ["frontend", "backend"],
            "poc",
            "myapp",
            ".kind",
            subdomain="moza",
            base_domain="rijksapp.dev",
            hostname_format=HostnameFormat.DOTS,
            domain_format="component-deployment-subdomain",
        )
        # Each component gets its own hostname, no root
        assert "frontend.poc.moza.rijksapp.dev" in hostnames
        assert "backend.poc.moza.rijksapp.dev" in hostnames
        # Root hostname (moza.rijksapp.dev) should NOT be added
        assert "moza.rijksapp.dev" not in hostnames

    def test_no_domain_format_adds_root_hostname(self):
        """Without domain_format, DOTS mode adds root hostname (backward compat)."""
        hostnames = get_deployment_hostnames(
            ["frontend"],
            "poc",
            "myapp",
            ".kind",
            subdomain="moza",
            base_domain="rijksapp.dev",
            hostname_format=HostnameFormat.DOTS,
            domain_format=None,
        )
        assert "frontend.moza.rijksapp.dev" in hostnames
        assert "moza.rijksapp.dev" in hostnames

    def test_domain_format_without_component_deduplicates(self):
        """Templates without {component} produce identical hostnames per component."""
        hostnames = get_deployment_hostnames(
            ["frontend", "backend"],
            "poc",
            "myapp",
            ".kind",
            subdomain="moza",
            base_domain="rijksapp.dev",
            hostname_format=HostnameFormat.DOTS,
            domain_format="deployment-subdomain",
        )
        # Both components produce poc.moza.rijksapp.dev, deduplicated
        assert hostnames == ["poc.moza.rijksapp.dev"]


# ---------------------------------------------------------------------------
# DomainFormatOptionsProvider
# ---------------------------------------------------------------------------


class TestDomainFormatOptionsProvider:
    def test_nice_url_mode_returns_dot_options(self):
        provider = DomainFormatOptionsProvider(domain_mode="nice-url")
        options = provider.get_options()
        values = [o["value"] for o in options]
        assert len(values) == 4
        # Dot options have dot-separated labels
        for opt in options:
            assert "." in opt["label"]

    def test_other_mode_returns_dash_options(self):
        provider = DomainFormatOptionsProvider(domain_mode="component-specific")
        options = provider.get_options()
        values = [o["value"] for o in options]
        assert len(values) == 4
        # Dash options have dash-separated labels
        for opt in options:
            assert "-" in opt["label"]

    def test_no_mode_returns_all_options(self):
        provider = DomainFormatOptionsProvider(domain_mode=None)
        options = provider.get_options()
        # Should return both dot and dash sets
        assert len(options) == 8

    def test_all_values_are_valid_template_ids(self):
        provider = DomainFormatOptionsProvider()
        for opt in provider.get_options():
            assert opt["value"] in DOMAIN_FORMAT_TEMPLATES


# ---------------------------------------------------------------------------
# DomainFormatValidator
# ---------------------------------------------------------------------------


class TestDomainFormatValidator:
    def test_valid_format(self):
        v = DomainFormatValidator()
        for fmt in DOMAIN_FORMAT_TEMPLATES:
            assert v.validate(fmt) == []

    def test_invalid_format(self):
        v = DomainFormatValidator()
        errors = v.validate("nonexistent-format")
        assert len(errors) == 1
        assert "Onbekend URL-formaat" in errors[0]

    def test_empty_value_passes(self):
        v = DomainFormatValidator()
        assert v.validate("") == []
        assert v.validate(None) == []


# ---------------------------------------------------------------------------
# DOMAIN_FORMAT_TEMPLATES integrity
# ---------------------------------------------------------------------------


class TestDomainFormatTemplates:
    def test_all_templates_have_two_variants(self):
        for key, val in DOMAIN_FORMAT_TEMPLATES.items():
            assert isinstance(val, tuple), f"{key} should be a tuple"
            assert len(val) == 2, f"{key} should have (dash, dot) pair"

    def test_dash_variant_uses_hyphens(self):
        for key, (dash, _dot) in DOMAIN_FORMAT_TEMPLATES.items():
            # The dash variant joins prefix parts with hyphens before the dot+domain
            parts = dash.split(".{domain}")
            prefix = parts[0]
            # Should not contain dots in prefix (except {domain} placeholder)
            assert "." not in prefix, f"{key} dash variant should not have dots in prefix"

    def test_dot_variant_uses_dots(self):
        for key, (_dash, dot) in DOMAIN_FORMAT_TEMPLATES.items():
            # The dot variant uses dots to separate parts
            parts = dot.split(".{domain}")
            prefix = parts[0]
            # Should not contain hyphens between template variables
            # (variables themselves may contain hyphens in their values)
            assert "-" not in prefix.replace("{", "").replace("}", ""), (
                f"{key} dot variant should use dots, not hyphens between variables"
            )

    def test_default_format_mapping_covers_all_modes(self):
        expected_modes = {"nice-url", "component-specific", "deployment-name", "custom"}
        assert set(DOMAIN_MODE_DEFAULT_FORMAT.keys()) == expected_modes

    def test_default_format_values_are_valid(self):
        for mode, fmt in DOMAIN_MODE_DEFAULT_FORMAT.items():
            assert fmt in DOMAIN_FORMAT_TEMPLATES, f"Default format '{fmt}' for mode '{mode}' not in templates"
