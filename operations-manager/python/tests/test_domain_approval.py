"""
Tests for the unified domain approval check and ingress fallback.

Tests is_deployment_domain_approved(), is_domain_format_dot_based(),
and apply_domain_approval_fallback().
"""

from opi.connectors.subdomain import is_deployment_domain_approved, is_domain_format_dot_based
from opi.utils.naming import SAFE_FALLBACK_FORMAT, apply_domain_approval_fallback

# ---------------------------------------------------------------------------
# is_deployment_domain_approved
# ---------------------------------------------------------------------------


class TestIsDeploymentDomainApproved:
    def test_cluster_default_always_approved(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        # sandbox.rijksapp.dev is the cluster domain (ingress_postfix = .sandbox.rijksapp.dev)
        assert is_deployment_domain_approved({}, "sandbox.rijksapp.dev", "anything", "sandboxed-local") is True

    def test_no_base_domain_always_approved(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        assert is_deployment_domain_approved({}, None, None, "sandboxed-local") is True

    def test_platform_domain_approved_without_subdomain(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "rijks.app", "status": "approved"}],
            }
        }
        assert is_deployment_domain_approved(project_data, "rijks.app", None, "odcn-production") is True

    def test_platform_domain_not_in_allowed_domains(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        assert is_deployment_domain_approved({}, "rijks.app", None, "odcn-production") is False

    def test_platform_domain_with_approved_subdomain(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "rijks.app", "status": "approved"}],
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [{"name": "wies", "status": "approved"}],
                    }
                ],
            }
        }
        assert is_deployment_domain_approved(project_data, "rijks.app", "wies", "odcn-production") is True

    def test_platform_domain_with_requested_subdomain_not_approved(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "rijks.app", "status": "approved"}],
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [{"name": "wies", "status": "requested"}],
                    }
                ],
            }
        }
        assert is_deployment_domain_approved(project_data, "rijks.app", "wies", "odcn-production") is False

    def test_platform_domain_with_unknown_subdomain_not_approved(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "rijks.app", "status": "approved"}],
            }
        }
        assert is_deployment_domain_approved(project_data, "rijks.app", "unknown", "odcn-production") is False

    def test_custom_domain_approved(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "mijn-app.nl", "status": "approved"}],
            }
        }
        assert is_deployment_domain_approved(project_data, "mijn-app.nl", None, "odcn-production") is True

    def test_custom_domain_requested_not_approved(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "mijn-app.nl", "status": "requested"}],
            }
        }
        assert is_deployment_domain_approved(project_data, "mijn-app.nl", None, "odcn-production") is False

    def test_unknown_domain_not_approved(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        assert is_deployment_domain_approved({}, "unknown.com", None, "odcn-production") is False


# ---------------------------------------------------------------------------
# is_domain_format_dot_based
# ---------------------------------------------------------------------------


class TestIsDomainFormatDotBased:
    def test_dot_formats(self):
        assert is_domain_format_dot_based("component.subdomain") is True
        assert is_domain_format_dot_based("deployment.subdomain") is True
        assert is_domain_format_dot_based("component.deployment.subdomain") is True
        assert is_domain_format_dot_based("component.deployment.project") is True
        assert is_domain_format_dot_based("deployment.project") is True

    def test_dash_formats(self):
        assert is_domain_format_dot_based("component-subdomain") is False
        assert is_domain_format_dot_based("deployment-subdomain") is False
        assert is_domain_format_dot_based("component-deployment-subdomain") is False
        assert is_domain_format_dot_based("component-deployment-project") is False
        assert is_domain_format_dot_based("deployment-project") is False

    def test_single_part_format(self):
        assert is_domain_format_dot_based("subdomain") is False


# ---------------------------------------------------------------------------
# apply_domain_approval_fallback
# ---------------------------------------------------------------------------


class TestApplyDomainApprovalFallback:
    def test_approved_returns_original(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "rijks.app", "status": "approved"}],
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [{"name": "wies", "status": "approved"}],
                    }
                ],
            }
        }
        fmt, domain = apply_domain_approval_fallback(
            domain_format="component.subdomain",
            base_domain="rijks.app",
            subdomain="wies",
            ingress_postfix=".rijks.app",
            project_data=project_data,
            cluster="odcn-production",
        )
        assert fmt == "component.subdomain"
        assert domain == "rijks.app"

    def test_not_approved_falls_back(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        fmt, domain = apply_domain_approval_fallback(
            domain_format="component.subdomain",
            base_domain="rijks.app",
            subdomain="unauthorized",
            ingress_postfix=".rijksapps.nl",
            project_data={},
            cluster="odcn-production",
        )
        assert fmt == SAFE_FALLBACK_FORMAT
        assert domain == "rijksapps.nl"

    def test_cluster_default_domain_no_fallback(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        fmt, domain = apply_domain_approval_fallback(
            domain_format="subdomain",
            base_domain="sandbox.rijksapp.dev",
            subdomain="anything",
            ingress_postfix=".sandbox.rijksapp.dev",
            project_data={},
            cluster="sandboxed-local",
        )
        # Cluster default is always approved
        assert fmt == "subdomain"
        assert domain == "sandbox.rijksapp.dev"
