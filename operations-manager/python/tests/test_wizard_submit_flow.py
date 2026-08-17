"""
Test the exact wizard final submission flow step by step.

Traces every operation that happens to the data from merged wizard state
to final project YAML, documenting the order and verifying that the
subdomain request survives each step.

This test exists because the current flow is opaque:
- clear_hidden_depends_on
- apply_dependent_generators
- process_json_submission (strip_transients=False)
- cross-section enforcement
- _prune_empty_dicts
- run_hooks(PRE_SAVE)
- apply_generators
- _assemble_deployment

Each step can silently remove or modify data. This test makes the
flow explicit and traceable.
"""

from unittest.mock import AsyncMock

import pytest
from opi.connectors.subdomain import get_domains_config
from opi.forms.editables.editable import Editable, FormState, WidgetType
from opi.forms.editables.hooks import StripTransientsHook
from opi.forms.editables.lifecycle import collect_hooks, run_hooks
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import get_flow
from opi.forms.visualizers.visualizer import EditableVisualizer


def _build_wizard_merged_data() -> dict:
    """Simulate what get_merged_data() returns after the user completed all wizard steps.

    Includes a deployment with a subdomain on a restricted domain and the
    _request-subdomain checkbox checked.
    """
    return {
        "display-name": "Test Subdomain Project",
        "name": "test-subdomain-project",
        "users": [{"email": "test@example.com", "role": "admin"}],
        "components": [{"name": "frontend", "image": "nginx:latest"}],
        "services": [],
        "deployments": [
            {
                "name": "productie",
                # What the wizard's merged data holds since v2.7: the web address under the
                # service, the transient request checkbox still on the deployment itself.
                "services": [
                    {
                        "reference": "publish-on-web",
                        "config": {
                            "domain-format": "subdomain",
                            "base-domain": "sandbox.rijksapp.dev",
                            "subdomain": "mijn-test",
                        },
                    }
                ],
                "_request-subdomain": True,
            }
        ],
    }


def _get_all_editables():
    """Get all editables as the wizard submission does: from active sections."""
    flow = get_flow("create-project")
    all_editables = []
    for section in flow.sections:
        all_editables.extend(section.editables)
    return all_editables, flow


class TestWizardSubmitFlowTraced:
    """Step-by-step trace of the wizard submission pipeline."""

    @pytest.mark.asyncio
    async def test_step1_merged_data_has_request_subdomain(self):
        """Step 1: The merged wizard state has _request-subdomain=True."""
        yaml_data = _build_wizard_merged_data()
        assert yaml_data["deployments"][0]["_request-subdomain"] is True

    @pytest.mark.asyncio
    async def test_step2_clear_hidden_preserves_request_subdomain(self):
        """Step 2: clear_hidden_depends_on should NOT remove _request-subdomain.

        The checkbox has show_when=SubdomainNeedsRequestCondition (no depends_on),
        so clear_hidden_depends_on should skip it entirely.
        """
        yaml_data = _build_wizard_merged_data()
        all_editables, _ = _get_all_editables()
        processor = EditableFormProcessor()

        processor.clear_hidden_depends_on(all_editables, yaml_data)

        assert "_request-subdomain" in yaml_data["deployments"][0], (
            f"clear_hidden_depends_on removed _request-subdomain! Deployment data: {yaml_data['deployments'][0]}"
        )

    @pytest.mark.asyncio
    async def test_step3_apply_dependent_generators_preserves_request_subdomain(self):
        """Step 3: apply_dependent_generators should not affect _request-subdomain."""
        yaml_data = _build_wizard_merged_data()
        all_editables, _ = _get_all_editables()
        processor = EditableFormProcessor()

        processor.clear_hidden_depends_on(all_editables, yaml_data)
        processor.apply_dependent_generators(all_editables, yaml_data)

        assert "_request-subdomain" in yaml_data["deployments"][0], (
            "apply_dependent_generators removed _request-subdomain!"
        )

    @pytest.mark.asyncio
    async def test_step4_process_json_submission_preserves_request_subdomain(self, monkeypatch):
        """Step 4: process_json_submission(strip_transients=False) should keep _request-subdomain."""
        from opi.services.persistence.subdomain_registry import SubdomainConnector

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        monkeypatch.setattr(SubdomainConnector, "get_by_subdomain", AsyncMock(return_value=None))

        yaml_data = _build_wizard_merged_data()
        all_editables, _ = _get_all_editables()
        processor = EditableFormProcessor()

        processor.clear_hidden_depends_on(all_editables, yaml_data)
        processor.apply_dependent_generators(all_editables, yaml_data)

        final_data, errors = await processor.process_json_submission(
            yaml_data,
            all_editables,
            yaml_data,
            edit_mode=False,
            enforcer_context={"project_name": None, "edit_mode": False},
            strip_transients=False,
        )

        assert "_request-subdomain" in final_data["deployments"][0], (
            f"process_json_submission removed _request-subdomain! "
            f"Deployment: {final_data['deployments'][0]}, errors: {errors}"
        )

    @pytest.mark.asyncio
    async def test_step5_hooks_create_domains_entry(self, monkeypatch):
        """Step 5: run_hooks(PRE_SAVE) should create domains.allowed-subdomains."""
        from opi.services.persistence.subdomain_registry import SubdomainConnector

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "sandboxed-local"})())
        monkeypatch.setattr(SubdomainConnector, "get_by_subdomain", AsyncMock(return_value=None))

        yaml_data = _build_wizard_merged_data()
        all_editables, _ = _get_all_editables()
        processor = EditableFormProcessor()

        processor.clear_hidden_depends_on(all_editables, yaml_data)
        processor.apply_dependent_generators(all_editables, yaml_data)

        final_data, errors = await processor.process_json_submission(
            yaml_data,
            all_editables,
            yaml_data,
            edit_mode=False,
            enforcer_context={"project_name": None, "edit_mode": False},
            strip_transients=False,
        )

        # Verify _request-subdomain survived to this point
        assert "_request-subdomain" in final_data["deployments"][0], (
            f"_request-subdomain lost before hooks! Deployment: {final_data['deployments'][0]}"
        )

        # Run PRE_SAVE hooks
        strip_hook_editable = EditableVisualizer(
            editable=Editable(
                yaml_path="_system/strip-transients",
                hooks={FormState.PRE_SAVE: StripTransientsHook(all_editables)},
            ),
            widget=WidgetType.HIDDEN,
            label="",
        )
        all_with_system = [*all_editables, strip_hook_editable]

        # Verify hooks are found
        hooks = collect_hooks(all_with_system, FormState.PRE_SAVE)
        hook_names = [h[1].__class__.__name__ for h in hooks]
        assert "SubdomainRequestHook" in hook_names, f"SubdomainRequestHook not found! Hooks: {hook_names}"

        await run_hooks(FormState.PRE_SAVE, all_with_system, final_data)

        # Verify domains entry was created (now under the publish-on-web service config)
        domains = get_domains_config(final_data)
        assert domains is not None, (
            f"domains section not created by SubdomainRequestHook! Final data keys: {list(final_data.keys())}"
        )
        allowed = domains["allowed-subdomains"]
        assert len(allowed) == 1
        assert allowed[0]["domain"] == "sandbox.rijksapp.dev"
        assert allowed[0]["subdomains"][0]["name"] == "mijn-test"
        assert allowed[0]["subdomains"][0]["status"] == "requested"

        # Verify transient was stripped
        assert "_request-subdomain" not in final_data["deployments"][0], (
            "StripTransientsHook did not remove _request-subdomain!"
        )


class TestClearedFieldRoundtrip:
    """Clearing a remove_when_none field must survive extract -> store -> merge.

    Regression for the component-edit modal: clearing the aliases field
    left the old values visible in the review summary because the additive
    merge in get_merged_data() resurrected them from the template snapshot.
    """

    def _component_section_editables(self):
        from opi.forms.editables.fields.components import COMPONENT_NAME_EDITABLE
        from opi.services.catalog.aliases.editables import COMPONENT_ALIASES_EDITABLE

        return [
            EditableVisualizer(editable=COMPONENT_NAME_EDITABLE, widget=WidgetType.TEXT, label="Naam"),
            EditableVisualizer(editable=COMPONENT_ALIASES_EDITABLE, widget=WidgetType.TEXTAREA, label="Aliases"),
        ]

    def test_cleared_alias_is_tombstoned_and_removed_after_merge(self):
        from opi.forms.wizard.state import WizardState
        from opi.web.router_wizard import CLEARED_FIELD, _extract_section_data

        editables = self._component_section_editables()
        # process_json_submission already deleted 'aliases' (remove_when_none)
        submitted_yaml = {"components": [{"name": "web", "image": "nginx:1"}]}

        fragment = _extract_section_data(editables, submitted_yaml)
        assert fragment["components"][0]["aliases"] == CLEARED_FIELD

        state = WizardState(
            flow_id="modal-component-edit",
            current_step="components-edit",
            active_sections=["components-edit"],
            base_data={"components": [{"name": "web", "aliases": {"DB": "old"}, "image": "nginx:1"}]},
        )
        state.store_step_data("components-edit", fragment)

        merged = state.get_merged_data()
        assert "aliases" not in merged["components"][0]
        assert merged["components"][0]["name"] == "web"
        assert merged["components"][0]["image"] == "nginx:1"

    def test_untouched_alias_survives_roundtrip(self):
        from opi.forms.wizard.state import WizardState
        from opi.web.router_wizard import _extract_section_data

        editables = self._component_section_editables()
        submitted_yaml = {"components": [{"name": "web", "aliases": {"DB": "kept"}}]}

        fragment = _extract_section_data(editables, submitted_yaml)
        state = WizardState(
            flow_id="modal-component-edit",
            current_step="components-edit",
            active_sections=["components-edit"],
            base_data={"components": [{"name": "web", "aliases": {"DB": "old"}}]},
        )
        state.store_step_data("components-edit", fragment)

        merged = state.get_merged_data()
        assert merged["components"][0]["aliases"] == {"DB": "kept"}


class TestSummaryConverterContext:
    """The review summary must pass yaml_data as context_data to converter.view().

    Regression: the call used ``yaml_data=`` (a kwarg no converter accepts),
    the TypeError fallback called view() without context, and AGE-encrypted
    values rendered as raw ciphertext in the summary.
    """

    def test_format_value_passes_context_data(self):
        from opi.web.router_wizard import _format_value

        seen: dict = {}

        class RecordingConverter:
            def view(self, value, context_data=None):
                seen["context_data"] = context_data
                return "decrypted"

        editable = Editable(yaml_path="components[*]/user-env-vars", converter=RecordingConverter())
        vis = EditableVisualizer(editable=editable, widget=WidgetType.TEXTAREA, label="Env vars")

        yaml_data = {"config": {"age-private-key": "key-material"}}
        result = _format_value(vis, "AGE-ENCRYPTED-BLOB", yaml_data)

        assert result == "decrypted"
        assert seen["context_data"] is yaml_data
