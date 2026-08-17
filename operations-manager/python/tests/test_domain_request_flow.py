"""
Tests for the domain request flow — requesting approval for non-default domains.

Same pattern as subdomain requests: condition shows checkbox, hook creates
the allowed-domains entry on PRE_SAVE.
"""

import pytest
from opi.connectors.subdomain import ensure_domain_requests, get_domains_config
from opi.forms.editables.conditions import DomainNeedsRequestCondition
from opi.forms.editables.editable import FormState
from opi.forms.editables.hooks import DomainRequestHook
from opi.forms.editables.lifecycle import collect_hooks
from opi.forms.editables.reindex import materialize_wildcard_visualizer
from opi.forms.visualizers.wizard_sections import DOMAIN_SECTION


class TestDomainNeedsRequestCondition:
    def test_cluster_default_returns_false(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        condition = DomainNeedsRequestCondition()
        yaml_data = {"deployments": [{"base-domain": "sandbox.rijksapp.dev"}]}
        assert condition.check(yaml_data) is False

    def test_no_domain_returns_false(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        condition = DomainNeedsRequestCondition()
        yaml_data = {"deployments": [{}]}
        assert condition.check(yaml_data) is False

    def test_unapproved_platform_domain_returns_true(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        condition = DomainNeedsRequestCondition()
        yaml_data = {"deployments": [{"base-domain": "rijks.app"}]}
        assert condition.check(yaml_data) is True

    def test_approved_domain_returns_false(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        condition = DomainNeedsRequestCondition()
        yaml_data = {
            "deployments": [{"base-domain": "rijks.app"}],
            "domains": {"allowed-domains": [{"domain": "rijks.app", "status": "approved"}]},
        }
        assert condition.check(yaml_data) is False

    def test_requested_domain_returns_true(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        condition = DomainNeedsRequestCondition()
        yaml_data = {
            "deployments": [{"base-domain": "rijks.app"}],
            "domains": {"allowed-domains": [{"domain": "rijks.app", "status": "requested"}]},
        }
        assert condition.check(yaml_data) is True

    def test_resolves_default_domain_via_resolvers(self, monkeypatch):
        """When base-domain is None, resolvers provide the default domain."""
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        # Ensure resolver returns exactly the cluster default domain
        monkeypatch.setattr(
            "opi.connectors.subdomain.get_supported_base_domains",
            lambda cluster=None: {"sandbox.rijksapp.dev"},
        )
        condition = DomainNeedsRequestCondition()

        yaml_data = {"deployments": [{}]}  # No base-domain
        assert condition.check(yaml_data) is False  # Without resolvers: no domain, returns False

        # With resolver: resolves to sandbox.rijksapp.dev (cluster default) → False
        from opi.forms.editables.resolvers import ClusterDefaultDomain

        resolvers = {"deployments[0]/base-domain": ClusterDefaultDomain()}
        condition.set_resolvers(resolvers)
        assert condition.check(yaml_data) is False  # Cluster default → no request needed


class TestDomainRequestHook:
    @pytest.mark.asyncio
    async def test_creates_allowed_domain_entry(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        hook = DomainRequestHook()
        yaml_data = {
            "deployments": [
                {
                    "base-domain": "rijks.app",
                    "_request-domain": True,
                }
            ],
        }
        await hook.execute(yaml_data, {})

        domains = get_domains_config(yaml_data)
        assert domains is not None
        allowed = domains["allowed-domains"]
        assert len(allowed) == 1
        assert allowed[0]["domain"] == "rijks.app"
        assert allowed[0]["status"] == "requested"
        assert len(allowed[0]["history"]) == 1

    @pytest.mark.asyncio
    async def test_skips_when_checkbox_not_checked(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        hook = DomainRequestHook()
        yaml_data = {"deployments": [{"base-domain": "rijks.app"}]}
        await hook.execute(yaml_data, {})
        assert "domains" not in yaml_data

    @pytest.mark.asyncio
    async def test_skips_cluster_default(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())

        hook = DomainRequestHook()
        yaml_data = {
            "deployments": [{"base-domain": "sandbox.rijksapp.dev", "_request-domain": True}],
        }
        await hook.execute(yaml_data, {})
        assert "domains" not in yaml_data

    @pytest.mark.asyncio
    async def test_skips_already_registered(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        hook = DomainRequestHook()
        yaml_data = {
            "deployments": [{"base-domain": "rijks.app", "_request-domain": True}],
            "domains": {"allowed-domains": [{"domain": "rijks.app", "status": "requested"}]},
        }
        await hook.execute(yaml_data, {})
        # Should not add a duplicate
        assert len(yaml_data["domains"]["allowed-domains"]) == 1


class TestDomainHookInWizardFlow:
    @pytest.mark.asyncio
    async def test_domain_hook_found_on_materialized_section(self):
        """DomainRequestHook is found after materializing domain section editables."""
        materialized = [materialize_wildcard_visualizer(e, 0) for e in DOMAIN_SECTION.editables]
        hooks = collect_hooks(materialized, FormState.PRE_SAVE)
        hook_names = [h[1].__class__.__name__ for h in hooks]
        assert "DomainRequestHook" in hook_names
        assert "SubdomainRequestHook" in hook_names


class TestSubdomainRequestOnClusterDefaultDomain:
    """A subdomain on the cluster's own domain must still need approval.

    ``_resolve_missing_base_domains`` deliberately does not persist
    ``base-domain`` when it equals the cluster default, so the field stays
    absent. ``ensure_domain_requests`` then read that empty value as "nothing
    to do" and skipped the whole deployment, including the subdomain branch
    that is explicitly written for the cluster-default case (the domain-level
    request is skipped via ``is_cluster_default``, the subdomain one is not).
    Result: every subdomain request on the cluster domain vanished silently.
    """

    def test_creates_subdomain_request_without_base_domain(self) -> None:
        project_data = {
            "deployments": [
                {"name": "productie", "domain-format": "subdomain", "subdomain": "vlam"},
            ],
        }
        ensure_domain_requests(project_data, "sandboxed-local")

        domains = get_domains_config(project_data)
        assert domains is not None, "no domains section was created"
        entries = domains["allowed-subdomains"]
        assert len(entries) == 1
        assert entries[0]["domain"] == "sandbox.rijksapp.dev"
        subdomains = entries[0]["subdomains"]
        assert len(subdomains) == 1
        assert subdomains[0]["name"] == "vlam"
        assert subdomains[0]["status"] == "requested"

    def test_does_not_create_a_domain_request_for_the_cluster_domain(self) -> None:
        """The cluster's own domain needs no approval; only the subdomain does."""
        project_data = {
            "deployments": [
                {"name": "productie", "domain-format": "subdomain", "subdomain": "vlam"},
            ],
        }
        ensure_domain_requests(project_data, "sandboxed-local")

        domains = get_domains_config(project_data)
        assert domains is not None
        assert domains.get("allowed-domains", []) == []
