"""
End-to-end test for the subdomain request flow through the wizard submission pipeline.

Simulates the exact code path that runs when the wizard is submitted,
including the case where base-domain is None (user never interacted with
the select, so the default domain was never stored).
"""

from unittest.mock import AsyncMock

import pytest
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
        from opi.connectors.subdomain import SubdomainConnector

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

        assert "domains" in yaml_data
        assert yaml_data["domains"]["allowed-subdomains"][0]["subdomains"][0]["name"] == "mijn-test"
        assert "_request-subdomain" not in yaml_data["deployments"][0]

    @pytest.mark.asyncio
    async def test_flow_with_none_base_domain_resolves_default(self, monkeypatch):
        """When base-domain is None (user didn't interact with select), the hook
        resolves it via transient_value_when_none and still creates the domains entry.

        This is the actual production scenario — the select shows the default
        domain but the value is never stored in wizard state.
        """
        from opi.connectors.subdomain import SubdomainConnector

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        monkeypatch.setattr(SubdomainConnector, "get_by_subdomain", AsyncMock(return_value=None))

        yaml_data = {
            "deployments": [
                {
                    "name": "productie",
                    "domain-format": "subdomain",
                    "base-domain": None,  # NOT set — user never interacted with select
                    "subdomain": "mijn-test",
                    "_request-subdomain": True,
                }
            ],
        }

        all_editables, materialized = _build_all_editables()
        context = {"resolvers": build_resolver_map(materialized)}
        await run_hooks(FormState.PRE_SAVE, all_editables, yaml_data, context)

        assert "domains" in yaml_data, (
            "SubdomainRequestHook should resolve the default domain via "
            "transient_value_when_none and create the domains entry"
        )
        allowed = yaml_data["domains"]["allowed-subdomains"]
        assert len(allowed) == 1
        # The domain is one of the cluster's supported domains (resolved from cluster config)
        assert isinstance(allowed[0]["domain"], str)
        assert len(allowed[0]["domain"]) > 0
        assert allowed[0]["subdomains"][0]["name"] == "mijn-test"
        assert allowed[0]["subdomains"][0]["status"] == "requested"

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
