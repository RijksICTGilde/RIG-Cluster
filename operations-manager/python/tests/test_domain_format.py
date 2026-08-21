"""
Tests for the domain-format feature.

Tests the configurable hostname template system:
- Template resolution (dash and dot variants)
- resolve_domain_tail helper
- get_component_ingress_map with domain_format parameter
- get_deployment_hostnames with domain_format parameter
- Backward compatibility (domain_format=None)
- DomainFormatOptionsProvider
- DomainFormatValidator
- Per-domain dot support
- Cross-step visibility (path/rewrite-path)
"""

import pytest
from opi.core.cluster_config import get_domain_supports_dots, get_nice_url_supported_domains
from opi.forms.editables.validators import DomainFormatValidator
from opi.forms.visualizers.providers import DomainFormatOptionsProvider
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path
from opi.utils.naming import (
    DOMAIN_FORMAT_TEMPLATES,
    generate_hostname_from_format,
    get_component_ingress_map,
    get_deployment_hostnames,
    resolve_domain_tail,
)

# Default cluster and pre-approved project data for tests that don't test approval.
_CLUSTER = "local"
_NO_APPROVAL = {}  # Cluster default domains don't need approval


def _approved(*domains: str) -> dict:
    """Create project_data with the given domains pre-approved."""
    return {"domains": {"allowed-domains": [{"domain": d, "status": "approved"} for d in domains]}}


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
    """Test each template ID produces expected hostnames."""

    def test_component_deployment_project_dashes(self):
        result = generate_hostname_from_format("component-deployment-project", "frontend", "poc", "myapp", None, "kind")
        assert result == "frontend-poc-myapp.kind"

    def test_component_deployment_project_dots(self):
        result = generate_hostname_from_format(
            "component.deployment.project", "frontend", "poc", "myapp", None, "rijksapp.dev"
        )
        assert result == "frontend.poc.myapp.rijksapp.dev"

    def test_component_deployment_subdomain_dashes(self):
        result = generate_hostname_from_format(
            "component-deployment-subdomain", "frontend", "poc", "myapp", "moza", "kind"
        )
        assert result == "frontend-poc-moza.kind"

    def test_component_deployment_subdomain_dots(self):
        result = generate_hostname_from_format(
            "component.deployment.subdomain", "frontend", "poc", "myapp", "moza", "rijksapp.dev"
        )
        assert result == "frontend.poc.moza.rijksapp.dev"

    def test_deployment_project_dashes(self):
        result = generate_hostname_from_format("deployment-project", "frontend", "poc", "myapp", None, "kind")
        assert result == "poc-myapp.kind"

    def test_deployment_project_dots(self):
        result = generate_hostname_from_format("deployment.project", "frontend", "poc", "myapp", None, "rijksapp.dev")
        assert result == "poc.myapp.rijksapp.dev"

    def test_deployment_subdomain_dashes(self):
        result = generate_hostname_from_format("deployment-subdomain", "frontend", "poc", "myapp", "moza", "kind")
        assert result == "poc-moza.kind"

    def test_deployment_subdomain_dots(self):
        result = generate_hostname_from_format(
            "deployment.subdomain", "frontend", "poc", "myapp", "moza", "rijksapp.dev"
        )
        assert result == "poc.moza.rijksapp.dev"

    def test_component_subdomain_dashes(self):
        result = generate_hostname_from_format("component-subdomain", "frontend", "poc", "myapp", "moza", "kind")
        assert result == "frontend-moza.kind"

    def test_component_subdomain_dots(self):
        result = generate_hostname_from_format(
            "component.subdomain", "frontend", "poc", "myapp", "moza", "rijksapp.dev"
        )
        assert result == "frontend.moza.rijksapp.dev"

    def test_subdomain_format(self):
        result = generate_hostname_from_format("subdomain", "frontend", "poc", "myapp", "moza", "kind")
        assert result == "moza.kind"

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
            project_data=_NO_APPROVAL,
            cluster=_CLUSTER,
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
            domain_format="deployment.subdomain",
            project_data=_approved("rijksapp.dev"),
            cluster=_CLUSTER,
        )
        assert result == {"poc-frontend": "poc.moza.rijksapp.dev"}

    def test_domain_format_uses_base_domain_as_domain(self):
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            base_domain="rijksapp.dev",
            domain_format="deployment.project",
            project_data=_approved("rijksapp.dev"),
            cluster=_CLUSTER,
        )
        assert result == {"poc-frontend": "poc.myapp.rijksapp.dev"}

    def test_domain_format_falls_back_to_ingress_postfix(self):
        result = get_component_ingress_map(
            "frontend",
            "poc",
            "myapp",
            ".kind",
            domain_format="deployment-project",
            project_data=_NO_APPROVAL,
            cluster=_CLUSTER,
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
            project_data=_NO_APPROVAL,
            cluster=_CLUSTER,
        )
        # Legacy: component-deployment-project.cluster
        assert result == {"poc-frontend": "frontend-poc-myapp.kind"}


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
            domain_format="component.deployment.subdomain",
            project_data=_approved("rijksapp.dev"),
            cluster=_CLUSTER,
        )
        # Each component gets its own hostname, no root
        assert "frontend.poc.moza.rijksapp.dev" in hostnames
        assert "backend.poc.moza.rijksapp.dev" in hostnames
        # Root hostname (moza.rijksapp.dev) should NOT be added
        assert "moza.rijksapp.dev" not in hostnames

    def test_root_component_on_dotted_format_adds_root_hostname(self):
        """A dotted format with a root component adds the root hostname."""
        hostnames = get_deployment_hostnames(
            ["frontend"],
            "poc",
            "myapp",
            ".kind",
            subdomain="moza",
            base_domain="rijksapp.dev",
            domain_format="component.subdomain",
            root_component="frontend",
            project_data=_approved("rijksapp.dev"),
            cluster=_CLUSTER,
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
            domain_format="deployment.subdomain",
            project_data=_approved("rijksapp.dev"),
            cluster=_CLUSTER,
        )
        # Both components produce poc.moza.rijksapp.dev, deduplicated
        assert hostnames == ["poc.moza.rijksapp.dev"]


# ---------------------------------------------------------------------------
# DomainFormatOptionsProvider
# ---------------------------------------------------------------------------


class TestDomainFormatOptionsProvider:
    def test_no_base_domain_returns_six_dash_options(self):
        provider = DomainFormatOptionsProvider()
        options = provider.get_options()
        assert len(options) == 6

    def test_all_values_are_valid_template_ids(self):
        provider = DomainFormatOptionsProvider()
        for opt in provider.get_options():
            assert opt["value"] in DOMAIN_FORMAT_TEMPLATES

    def test_expected_dash_values(self):
        provider = DomainFormatOptionsProvider()
        values = [o["value"] for o in provider.get_options()]
        assert "component-deployment-project" in values
        assert "component-deployment-subdomain" in values
        assert "deployment-project" in values
        assert "deployment-subdomain" in values
        assert "component-subdomain" in values
        assert "subdomain" in values

    def test_all_options_have_label(self):
        provider = DomainFormatOptionsProvider()
        for opt in provider.get_options():
            assert "label" in opt
            assert len(opt["label"]) > 0

    def test_supports_dots_returns_eleven_options(self):
        """When base_domain supports dots, dash + dot variants are shown."""
        provider = DomainFormatOptionsProvider(base_domain="kind", cluster="local")
        options = provider.get_options()
        assert len(options) == 11  # 6 dash + 5 dot (subdomain has no dot variant)

    def test_supports_dots_sorted_by_value(self):
        """Options are sorted alphabetically by value."""
        provider = DomainFormatOptionsProvider(base_domain="kind", cluster="local")
        options = provider.get_options()
        values = [o["value"] for o in options]
        assert values == sorted(values)

    def test_dot_values_are_valid_template_ids(self):
        provider = DomainFormatOptionsProvider(base_domain="kind", cluster="local")
        for opt in provider.get_options():
            assert opt["value"] in DOMAIN_FORMAT_TEMPLATES

    def test_labels_show_format_pattern(self):
        """Labels show the format pattern with .domein suffix."""
        provider = DomainFormatOptionsProvider()
        options = provider.get_options()
        cdp = next(o for o in options if o["value"] == "component-deployment-project")
        assert cdp["label"] == "component-deployment-project.domein"
        sub = next(o for o in options if o["value"] == "subdomain")
        assert sub["label"] == "subdomain.domein"

    def test_no_dots_support_returns_six(self):
        """When base_domain doesn't support dots, only dash variants."""
        provider = DomainFormatOptionsProvider(base_domain="nonexistent.domain", cluster="local")
        options = provider.get_options()
        assert len(options) == 6

    def test_sandbox_default_domain_no_dots(self):
        """sandbox.rijksapp.dev on sandboxed-local does not support dots."""
        provider = DomainFormatOptionsProvider(base_domain="sandbox.rijksapp.dev", cluster="sandboxed-local")
        options = provider.get_options()
        assert len(options) == 6

    def test_sandbox_test_domain_supports_dots(self):
        """robbertuittenbroek.nl on sandboxed-local supports dots."""
        provider = DomainFormatOptionsProvider(base_domain="robbertuittenbroek.nl", cluster="sandboxed-local")
        options = provider.get_options()
        assert len(options) == 11


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
    def test_all_templates_are_strings(self):
        for key, val in DOMAIN_FORMAT_TEMPLATES.items():
            assert isinstance(val, str), f"{key} should be a string template"

    def test_dash_formats_use_hyphens(self):
        dash_formats = {k: v for k, v in DOMAIN_FORMAT_TEMPLATES.items() if "-" in k or k == "subdomain"}
        for key, template in dash_formats.items():
            prefix = template.split(".{domain}")[0]
            assert "." not in prefix, f"{key} dash template should not have dots in prefix"

    def test_dot_formats_use_dots(self):
        dot_formats = {k: v for k, v in DOMAIN_FORMAT_TEMPLATES.items() if "." in k}
        for key, template in dot_formats.items():
            prefix = template.split(".{domain}")[0]
            assert "-" not in prefix.replace("{", "").replace("}", ""), (
                f"{key} dot template should use dots, not hyphens"
            )


# ---------------------------------------------------------------------------
# Per-domain dot support
# ---------------------------------------------------------------------------


class TestPerDomainDotSupport:
    def test_get_domain_supports_dots_true(self):
        assert get_domain_supports_dots("local", "kind") is True

    def test_get_domain_supports_dots_unknown_domain(self):
        assert get_domain_supports_dots("local", "nonexistent.domain") is False

    def test_get_nice_url_supported_domains_extracts_strings(self):
        domains = get_nice_url_supported_domains("local")
        assert "kind" in domains
        assert "local" in domains
        assert all(isinstance(d, str) for d in domains)

    def test_get_domain_supports_dots_production(self):
        assert get_domain_supports_dots("odcn-production", "rijks.app") is True


# ---------------------------------------------------------------------------
# Domain editables show_when configuration
# ---------------------------------------------------------------------------


class TestDomainEditablesShowWhen:
    def test_subdomain_shows_for_subdomain_formats(self):
        from opi.forms.editables.fields.domains import DOMAIN_SUBDOMAIN_EDITABLE
        from opi.utils.naming import SUBDOMAIN_FORMAT_IDS

        assert DOMAIN_SUBDOMAIN_EDITABLE.depends_on == domain_setting_path(DomainSetting.DOMAIN_FORMAT)
        assert DOMAIN_SUBDOMAIN_EDITABLE.show_when == {"value": SUBDOMAIN_FORMAT_IDS}
        # Verify expected formats are included
        assert "component-deployment-subdomain" in SUBDOMAIN_FORMAT_IDS
        assert "deployment-subdomain" in SUBDOMAIN_FORMAT_IDS
        assert "subdomain" in SUBDOMAIN_FORMAT_IDS
        assert "component.deployment.subdomain" in SUBDOMAIN_FORMAT_IDS
        # Non-subdomain formats are excluded
        assert "component-deployment-project" not in SUBDOMAIN_FORMAT_IDS

    def test_base_domain_always_visible(self):
        from opi.forms.editables.fields.domains import DOMAIN_BASE_DOMAIN_EDITABLE

        assert DOMAIN_BASE_DOMAIN_EDITABLE.depends_on is None
        assert DOMAIN_BASE_DOMAIN_EDITABLE.show_when is None

    def test_root_component_shows_for_dot_component_formats(self):
        from opi.forms.editables.fields.domains import DOMAIN_ROOT_COMPONENT_EDITABLE
        from opi.utils.naming import ROOT_COMPONENT_FORMAT_IDS

        assert DOMAIN_ROOT_COMPONENT_EDITABLE.depends_on == domain_setting_path(DomainSetting.DOMAIN_FORMAT)
        assert DOMAIN_ROOT_COMPONENT_EDITABLE.show_when == {"value": ROOT_COMPONENT_FORMAT_IDS}
        # Only dot formats with {component} qualify
        assert "component.deployment.project" in ROOT_COMPONENT_FORMAT_IDS
        assert "component.deployment.subdomain" in ROOT_COMPONENT_FORMAT_IDS
        assert "component.subdomain" in ROOT_COMPONENT_FORMAT_IDS
        # Dash formats and formats without {component} are excluded
        assert "component-deployment-project" not in ROOT_COMPONENT_FORMAT_IDS
        assert "deployment-project" not in ROOT_COMPONENT_FORMAT_IDS
        assert "subdomain" not in ROOT_COMPONENT_FORMAT_IDS

    def test_domain_format_is_required_with_default(self):
        from opi.forms.editables.fields.domains import DOMAIN_FORMAT_EDITABLE

        assert DOMAIN_FORMAT_EDITABLE.required is True
        assert DOMAIN_FORMAT_EDITABLE.default == "component-deployment-project"
