"""
End-to-end test for the subdomain request flow through the wizard submission pipeline.

Simulates the exact code path that runs when the wizard is submitted,
including the case where base-domain is None (user never interacted with
the select, so the default domain was never stored).
"""

from unittest.mock import AsyncMock

import pytest
from opi.connectors.subdomain import get_domains_config
from opi.forms.editables.editable import Editable, FormState, WidgetType
from opi.forms.editables.hooks import StripTransientsHook
from opi.forms.editables.lifecycle import collect_hooks, run_hooks
from opi.forms.editables.reindex import materialize_wildcard_visualizer
from opi.forms.editables.resolvers import build_resolver_map
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.visualizers.wizard_sections import DOMAIN_SECTION


def _build_all_editables():
    """Build materialized editables + system hooks, like the wizard does."""
    materialized = [materialize_wildcard_visualizer(e, 0) for e in DOMAIN_SECTION.editables]
    strip_hook = EditableVisualizer(
        editable=Editable(
            yaml_path="_system/strip-transients",
            hooks={FormState.PRE_SAVE: StripTransientsHook(materialized)},
        ),
        widget=WidgetType.HIDDEN,
        label="",
    )
    return [*materialized, strip_hook], materialized


class TestWizardSubdomainFlow:
    @pytest.mark.asyncio
    async def test_hooks_found_on_materialized_domain_section(self):
        """SubdomainRequestHook is found after materializing domain section editables."""
        all_editables, _ = _build_all_editables()
        hooks = collect_hooks(all_editables, FormState.PRE_SAVE)
        hook_names = [h[1].__class__.__name__ for h in hooks]
        assert "SubdomainRequestHook" in hook_names
        assert "StripTransientsHook" in hook_names

    @pytest.mark.asyncio
    async def test_flow_with_explicit_base_domain(self, monkeypatch):
        """When base-domain is explicitly set, the hook creates the domains entry."""
        from opi.services.persistence.subdomain_registry import SubdomainConnector

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        monkeypatch.setattr(SubdomainConnector, "get_by_subdomain", AsyncMock(return_value=None))

        yaml_data = {
            "deployments": [
                {
                    "name": "productie",
                    "domain-format": "subdomain",
                    "base-domain": "sandbox.rijksapp.dev",
                    "subdomain": "mijn-test",
                    "_request-subdomain": True,
                }
            ],
        }

        all_editables, materialized = _build_all_editables()
        context = {"resolvers": build_resolver_map(materialized)}
        await run_hooks(FormState.PRE_SAVE, all_editables, yaml_data, context)

        assert get_domains_config(yaml_data) is not None
        assert get_domains_config(yaml_data)["allowed-subdomains"][0]["subdomains"][0]["name"] == "mijn-test"
        assert "_request-subdomain" not in yaml_data["deployments"][0]

    @pytest.mark.asyncio
    async def test_none_base_domain_stays_cluster_default_no_request(self, monkeypatch):
        """When base-domain is left as the cluster default (None), the hook must
        NOT materialise a domain the user didn't pick and must NOT create a
        subdomain request: the cluster default is always usable without approval
        (is_deployment_domain_approved returns True for an empty base-domain).

        Regression guard for the wizard bug where selecting "cluster default"
        (the empty option) kept the last-selected nice-URL domain (e.g.
        rijks.app), wrote it into the project file plus a phantom request, and
        deployed to a domain the user never chose.
        """
        from opi.services.persistence.subdomain_registry import SubdomainConnector

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        monkeypatch.setattr(SubdomainConnector, "get_by_subdomain", AsyncMock(return_value=None))

        yaml_data = {
            "deployments": [
                {
                    "name": "productie",
                    "domain-format": "subdomain",
                    "base-domain": None,  # cluster default — user never picked a domain
                    "subdomain": "mijn-test",
                    "_request-subdomain": True,
                }
            ],
        }

        all_editables, materialized = _build_all_editables()
        context = {"resolvers": build_resolver_map(materialized)}
        await run_hooks(FormState.PRE_SAVE, all_editables, yaml_data, context)

        # The cluster default is never materialised into the deployment...
        assert yaml_data["deployments"][0].get("base-domain") is None
        # ...and no phantom subdomain/domain request is created for it.
        assert "domains" not in yaml_data

    @pytest.mark.asyncio
    async def test_flow_without_checkbox_no_domains(self, monkeypatch):
        """When checkbox is not checked, no domains entry is created."""
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())

        yaml_data = {
            "deployments": [
                {
                    "name": "productie",
                    "domain-format": "subdomain",
                    "base-domain": "sandbox.rijksapp.dev",
                    "subdomain": "mijn-test",
                    # _request-subdomain NOT set
                }
            ],
        }

        all_editables, materialized = _build_all_editables()
        context = {"resolvers": build_resolver_map(materialized)}
        await run_hooks(FormState.PRE_SAVE, all_editables, yaml_data, context)

        assert "domains" not in yaml_data
