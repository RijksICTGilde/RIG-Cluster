"""Test project-level service selection/deselection through wizard flows.

Verifies that the virtualize pattern correctly isolates service selection
(``services``) from per-service config (``_services-config``) in step_data,
preventing collisions during get_merged_data() in both the create wizard
and the modal edit wizard.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.visualizers.flows import (
    CREATE_FLOW,
    MODAL_EDIT_SERVICES_FLOW,
)
from opi.forms.visualizers.wizard_sections import _extract_services
from opi.forms.wizard.resolver import resolve_active_section_ids
from opi.forms.wizard.state import WizardState
from opi.web.router_wizard import _extract_section_data, _split_data_across_sections

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KEYCLOAK_CONFIG = {
    "keycloak": {
        "config": {
            "template": "sso-support",
            "restrict-access": {
                "enabled": True,
                "realm-role": "allowed-user",
                "error-message": "${accessDeniedNoPermission}",
            },
        },
    },
}

POSTGRESQL_CONFIG = {
    "namespace-postgresql-database": {
        "config": {
            "instances": 2,
            "storage": "5Gi",
        },
    },
}


@pytest.fixture
def project_data_with_services() -> dict[str, Any]:
    """Project data with keycloak and postgresql services (with config dicts)."""
    return {
        "display-name": "Test Project",
        "services": [
            copy.deepcopy(KEYCLOAK_CONFIG),
            "publish-on-web",
            copy.deepcopy(POSTGRESQL_CONFIG),
            "metrics-scraper",
        ],
        "components": [
            {
                "name": "frontend",
                "services": ["publish-on-web", "keycloak"],
            },
        ],
        "deployments": [{"name": "test", "domain-format": "nice-url"}],
    }


@pytest.fixture
def edit_flow():
    """The modal-edit-services flow."""
    return MODAL_EDIT_SERVICES_FLOW


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _service_names(services: list) -> list[str]:
    """Extract plain service names from a mixed string/dict list."""
    return _extract_services({"services": services})


def _make_state(
    flow_id: str,
    step_data: dict[str, dict[str, Any]],
    active_sections: list[str],
    virt_mappings: dict[str, str] | None = None,
) -> WizardState:
    """Create a WizardState with the given step data."""
    state = WizardState(
        flow_id=flow_id,
        current_step=active_sections[0] if active_sections else "",
        active_sections=active_sections,
        step_data=step_data,
        virt_mappings=virt_mappings or {},
    )
    return state


# ===========================================================================
# _split_data_across_sections tests (edit wizard prefill)
# ===========================================================================


class TestSplitDataAcrossSections:
    """Test that _split_data_across_sections stores config under virtual keys."""

    def test_services_edit_gets_plain_services(self, project_data_with_services, edit_flow):
        """The services-edit section should get the services list."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)
        assert "services-edit" in step_data
        assert "services" in step_data["services-edit"]

    def test_keycloak_config_uses_virtual_key(self, project_data_with_services, edit_flow):
        """Keycloak-config should store under _services-config, NOT services."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)
        if "keycloak-config" in step_data:
            assert "services" not in step_data["keycloak-config"], (
                "keycloak-config should NOT have 'services' key — use virtual key"
            )
            assert "_services-config" in step_data["keycloak-config"]

    def test_postgresql_config_uses_virtual_key(self, project_data_with_services, edit_flow):
        """PostgreSQL-config should store under _services-config, NOT services."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)
        if "postgresql-config" in step_data:
            assert "services" not in step_data["postgresql-config"]
            assert "_services-config" in step_data["postgresql-config"]

    def test_no_collision_between_sections(self, project_data_with_services, edit_flow):
        """services-edit has 'services', config sections have '_services-config'."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)
        services_edit_keys = set(step_data.get("services-edit", {}).keys())
        keycloak_keys = set(step_data.get("keycloak-config", {}).keys())
        assert not services_edit_keys & keycloak_keys, f"Key collision: {services_edit_keys & keycloak_keys}"


# ===========================================================================
# _extract_section_data tests (POST step data extraction)
# ===========================================================================


class TestExtractSectionData:
    """Test that _extract_section_data uses virtual keys for virtualized editables."""

    def test_services_section_uses_real_key(self):
        """Non-virtualized editables store under real key."""
        from opi.forms.visualizers.wizard_sections import SERVICES_EDIT_SECTION

        submitted = {"services": ["keycloak", "publish-on-web"]}
        section_data = _extract_section_data(SERVICES_EDIT_SECTION.editables, submitted)
        assert "services" in section_data
        assert section_data["services"] == ["keycloak", "publish-on-web"]

    def test_keycloak_config_uses_virtual_key(self):
        """Virtualized editables store under virtual key (_services-config)."""
        from opi.forms.visualizers.wizard_sections import KEYCLOAK_CONFIG_SECTION

        submitted = {
            "services": [
                {"keycloak": {"config": {"template": "sso-support"}}},
                "publish-on-web",
            ],
        }
        section_data = _extract_section_data(KEYCLOAK_CONFIG_SECTION.editables, submitted)
        assert "_services-config" in section_data, (
            f"Expected virtual key '_services-config', got keys: {list(section_data.keys())}"
        )
        assert "services" not in section_data, "Virtualized editables should NOT store under real key"


# ===========================================================================
# WizardState.get_merged_data devirtualization tests
# ===========================================================================


class TestGetMergedDataDevirtualize:
    """Test that get_merged_data folds virtual keys back into real keys."""

    def test_devirtualize_restores_config_dicts(self):
        """Config dicts from _services-config replace plain strings in services."""
        state = _make_state(
            flow_id="modal-edit-services",
            step_data={
                "services-edit": {
                    "services": ["keycloak", "publish-on-web", "metrics-scraper"],
                },
                "keycloak-config": {
                    "_services-config": [
                        {"keycloak": {"config": {"template": "sso-support"}}},
                        "publish-on-web",
                    ],
                },
            },
            active_sections=["services-edit", "keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()

        # Should NOT have the virtual key
        assert "_services-config" not in merged

        # services should be a list with keycloak restored as a config dict
        services = merged["services"]
        names = _service_names(services)
        assert "keycloak" in names
        assert "publish-on-web" in names
        assert "metrics-scraper" in names

        # keycloak should be a dict (with config), not a plain string
        keycloak_entry = next((s for s in services if isinstance(s, dict) and "keycloak" in s), None)
        assert keycloak_entry is not None, f"keycloak should be a config dict, got services: {services}"
        assert keycloak_entry["keycloak"]["config"]["template"] == "sso-support"

    def test_devirtualize_preserves_plain_strings(self):
        """Services without config in _services-config stay as plain strings."""
        state = _make_state(
            flow_id="modal-edit-services",
            step_data={
                "services-edit": {
                    "services": ["keycloak", "publish-on-web"],
                },
            },
            active_sections=["services-edit"],
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()
        assert "publish-on-web" in merged["services"]

    def test_no_virt_mappings_is_noop(self):
        """Without virt_mappings, get_merged_data works as before."""
        state = _make_state(
            flow_id="test",
            step_data={"a": {"services": ["keycloak"]}},
            active_sections=["a"],
        )
        merged = state.get_merged_data()
        assert merged == {"services": ["keycloak"]}

    def test_virt_mappings_roundtrip_serialization(self):
        """virt_mappings survives to_dict/from_dict."""
        state = _make_state(
            flow_id="test",
            step_data={},
            active_sections=[],
            virt_mappings={"_services-config": "services"},
        )
        restored = WizardState.from_dict(state.to_dict())
        assert restored.virt_mappings == {"_services-config": "services"}


# ===========================================================================
# Visibility tests (conditional config sections)
# ===========================================================================


class TestServiceVisibility:
    """Test that config sections show/hide based on service selection."""

    def test_keycloak_config_visible_when_selected(self, edit_flow):
        """When keycloak is in the services list, keycloak-config is active."""
        step_data = {
            "services-edit": {"services": ["keycloak", "publish-on-web"]},
        }
        active = resolve_active_section_ids(edit_flow, step_data)
        assert "keycloak-config" in active

    def test_keycloak_config_hidden_when_deselected(self, edit_flow):
        """When keycloak is NOT in the services list, keycloak-config is hidden."""
        step_data = {
            "services-edit": {"services": ["publish-on-web"]},
        }
        active = resolve_active_section_ids(edit_flow, step_data)
        assert "keycloak-config" not in active

    def test_postgresql_config_visible_when_selected(self, edit_flow):
        step_data = {
            "services-edit": {"services": ["namespace-postgresql-database"]},
        }
        active = resolve_active_section_ids(edit_flow, step_data)
        assert "postgresql-config" in active

    def test_postgresql_config_hidden_when_deselected(self, edit_flow):
        step_data = {
            "services-edit": {"services": ["keycloak"]},
        }
        active = resolve_active_section_ids(edit_flow, step_data)
        assert "postgresql-config" not in active

    def test_visibility_not_polluted_by_config_step_data(self, edit_flow):
        """Config sections using virtual keys should NOT pollute service visibility.

        This is the core bug fix: previously keycloak-config stored its data
        under 'services' key, which could overwrite the services-edit selection
        in _merge_step_data, causing keycloak-config to stay visible even when
        keycloak was deselected.
        """
        step_data = {
            # User deselected keycloak
            "services-edit": {"services": ["publish-on-web"]},
            # Config section still has stale data under virtual key
            "keycloak-config": {
                "_services-config": [
                    {"keycloak": {"config": {"template": "sso-support"}}},
                ],
            },
        }
        active = resolve_active_section_ids(edit_flow, step_data)
        assert "keycloak-config" not in active, (
            "keycloak-config should be hidden when keycloak is deselected, "
            "even if config step_data exists under virtual key"
        )


# ===========================================================================
# Full edit wizard flow simulation
# ===========================================================================


class TestEditWizardServiceFlow:
    """Simulate the full modal-edit-services wizard flow."""

    def test_select_new_service(self, project_data_with_services, edit_flow):
        """Selecting a new service in the edit wizard should persist it."""
        # Step 1: Split existing data across sections
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)

        # Step 2: User adds authorization-wall to the services list
        original_services = _service_names(project_data_with_services["services"])
        new_services = [*original_services, "authorization-wall"]
        step_data["services-edit"] = {"services": new_services}

        # Step 3: Create state and merge
        state = _make_state(
            flow_id="modal-edit-services",
            step_data=step_data,
            active_sections=resolve_active_section_ids(edit_flow, step_data),
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()

        # The new service should be present
        merged_names = _service_names(merged["services"])
        assert "authorization-wall" in merged_names

        # Original services should still be present
        for svc in original_services:
            assert svc in merged_names, f"Original service {svc} lost after merge"

    def test_deselect_service(self, project_data_with_services, edit_flow):
        """Deselecting a service in the edit wizard should remove it."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)

        # User removes keycloak
        original_names = _service_names(project_data_with_services["services"])
        new_services = [s for s in original_names if s != "keycloak"]
        step_data["services-edit"] = {"services": new_services}

        state = _make_state(
            flow_id="modal-edit-services",
            step_data=step_data,
            active_sections=resolve_active_section_ids(edit_flow, step_data),
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()

        merged_names = _service_names(merged["services"])
        assert "keycloak" not in merged_names
        # Other services should remain
        assert "publish-on-web" in merged_names

    def test_deselect_all_services(self, project_data_with_services, edit_flow):
        """Deselecting all services should result in an empty list."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)
        step_data["services-edit"] = {"services": []}

        state = _make_state(
            flow_id="modal-edit-services",
            step_data=step_data,
            active_sections=resolve_active_section_ids(edit_flow, step_data),
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()
        assert merged["services"] == []

    def test_config_preserved_after_service_stays_selected(self, project_data_with_services, edit_flow):
        """When a service with config stays selected, its config dict is preserved."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)

        # User keeps all services (no change)
        original_names = _service_names(project_data_with_services["services"])
        step_data["services-edit"] = {"services": original_names}

        state = _make_state(
            flow_id="modal-edit-services",
            step_data=step_data,
            active_sections=resolve_active_section_ids(edit_flow, step_data),
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()

        # keycloak should still have its config dict
        keycloak_entry = next(
            (s for s in merged["services"] if isinstance(s, dict) and "keycloak" in s),
            None,
        )
        assert keycloak_entry is not None, f"keycloak config dict should be preserved, got: {merged['services']}"
        assert keycloak_entry["keycloak"]["config"]["template"] == "sso-support"

    def test_no_service_duplication_in_merged_data(self, project_data_with_services, edit_flow):
        """Services should never appear twice in the merged services list."""
        step_data = _split_data_across_sections(edit_flow, project_data_with_services)

        original_names = _service_names(project_data_with_services["services"])
        step_data["services-edit"] = {"services": original_names}

        state = _make_state(
            flow_id="modal-edit-services",
            step_data=step_data,
            active_sections=resolve_active_section_ids(edit_flow, step_data),
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()

        merged_names = _service_names(merged["services"])
        assert len(merged_names) == len(set(merged_names)), f"Duplicate services found: {merged_names}"


# ===========================================================================
# Full create wizard flow simulation
# ===========================================================================


class TestCreateWizardServiceFlow:
    """Simulate the service selection flow in the create wizard."""

    def _get_create_sections(self, step_data: dict) -> list[str]:
        return resolve_active_section_ids(CREATE_FLOW, step_data)

    def test_select_keycloak_shows_config(self):
        """Selecting keycloak in the create wizard should show keycloak-config."""
        step_data = {"services": {"services": ["keycloak", "publish-on-web"]}}
        active = self._get_create_sections(step_data)
        assert "keycloak-config" in active

    def test_deselect_keycloak_hides_config(self):
        """Deselecting keycloak should hide keycloak-config."""
        step_data = {"services": {"services": ["publish-on-web"]}}
        active = self._get_create_sections(step_data)
        assert "keycloak-config" not in active

    def test_select_postgresql_shows_config(self):
        step_data = {"services": {"services": ["namespace-postgresql-database"]}}
        active = self._get_create_sections(step_data)
        assert "postgresql-config" in active

    def test_select_auth_wall_shows_config(self):
        step_data = {"services": {"services": ["authorization-wall"]}}
        active = self._get_create_sections(step_data)
        assert "auth-wall-config" in active

    def test_create_flow_service_step_no_collision_with_config(self):
        """In create wizard, service selection and config don't collide.

        Simulates: user selects services, then fills in keycloak config,
        then merges all data. The services list should contain the
        user-selected services (with config dicts from keycloak-config).
        """
        from opi.forms.visualizers.wizard_sections import (
            KEYCLOAK_CONFIG_SECTION,
            SERVICES_SECTION,
        )

        # Step 1: User selects services
        services_submitted = {"services": ["keycloak", "publish-on-web"]}
        services_section_data = _extract_section_data(SERVICES_SECTION.editables, services_submitted)

        # Step 2: User fills in keycloak config
        # (processor writes to real path services/keycloak/config/...)
        keycloak_submitted = {
            "services": [
                {"keycloak": {"config": {"template": "sso-support"}}},
                "publish-on-web",
            ],
        }
        keycloak_section_data = _extract_section_data(KEYCLOAK_CONFIG_SECTION.editables, keycloak_submitted)

        # Verify no collision: services section uses 'services',
        # keycloak-config uses '_services-config'
        assert "services" in services_section_data
        assert "_services-config" in keycloak_section_data
        assert "services" not in keycloak_section_data

        # Step 3: Merge via WizardState
        state = _make_state(
            flow_id="create-project",
            step_data={
                "services": services_section_data,
                "keycloak-config": keycloak_section_data,
            },
            active_sections=["services", "keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        merged = state.get_merged_data()

        # Services list should have both services
        names = _service_names(merged["services"])
        assert "keycloak" in names
        assert "publish-on-web" in names

        # keycloak should have config dict restored
        keycloak_entry = next(
            (s for s in merged["services"] if isinstance(s, dict) and "keycloak" in s),
            None,
        )
        assert keycloak_entry is not None

    def test_create_flow_deselect_then_reselect(self):
        """Deselecting then reselecting a service should restore config from stash."""
        state = WizardState(
            flow_id="create-project",
            current_step="services",
            active_sections=["identity", "services", "keycloak-config", "components"],
            completed_steps=["identity", "services", "keycloak-config"],
            step_data={
                "identity": {"display-name": "Test"},
                "services": {"services": ["keycloak"]},
                "keycloak-config": {
                    "_services-config": [
                        {"keycloak": {"config": {"template": "sso-support"}}},
                    ],
                },
            },
            virt_mappings={"_services-config": "services"},
        )

        # User deselects keycloak
        state.step_data["services"] = {"services": ["publish-on-web"]}
        new_active = resolve_active_section_ids(CREATE_FLOW, state.step_data)
        state.stash_inactive_sections(new_active)
        state.active_sections = new_active

        assert "keycloak-config" not in state.step_data
        assert "keycloak-config" in state.stashed_data

        # User re-selects keycloak
        state.step_data["services"] = {"services": ["keycloak", "publish-on-web"]}
        new_active = resolve_active_section_ids(CREATE_FLOW, state.step_data)
        state.stash_inactive_sections(new_active)
        state.active_sections = new_active

        assert "keycloak-config" in state.step_data
        assert "keycloak-config" not in state.stashed_data
        # Config should be restored from stash
        assert "_services-config" in state.step_data["keycloak-config"]


# ===========================================================================
# populate_virt_mappings tests
# ===========================================================================


class TestPopulateVirtMappings:
    """Test that populate_virt_mappings extracts mappings from flow sections."""

    def test_edit_services_flow_has_mapping(self):
        state = WizardState(flow_id="test", current_step="x", active_sections=[])
        state.populate_virt_mappings(MODAL_EDIT_SERVICES_FLOW.sections)
        assert "_services-config" in state.virt_mappings
        assert state.virt_mappings["_services-config"] == "services"

    def test_create_flow_has_mapping(self):
        state = WizardState(flow_id="test", current_step="x", active_sections=[])
        state.populate_virt_mappings(CREATE_FLOW.sections)
        assert "_services-config" in state.virt_mappings

    def test_flow_without_virtualize_has_empty_mappings(self):
        """Flows without virtualized editables should have no mappings."""
        from opi.forms.visualizers.flows import MODAL_EDIT_TEAM_FLOW

        state = WizardState(flow_id="test", current_step="x", active_sections=[])
        state.populate_virt_mappings(MODAL_EDIT_TEAM_FLOW.sections)
        assert state.virt_mappings == {}


# ===========================================================================
# Standalone service config edit flow tests
#
# These test the single-section edit flows (keycloak, postgresql, auth-wall)
# that are opened from the project detail page. They previously had three
# independent bugs:
#   1. Display values missing (devirtualize produced dict, not list)
#   2. post_save_action defaulted to "save_only" (no project refresh)
#   3. Visibility lambda failed at save time (section not in active_sections)
# ===========================================================================


class TestStandaloneKeycloakEditFlow:
    """Test the standalone keycloak config edit flow (modal-edit-keycloak-config)."""

    @pytest.fixture
    def keycloak_flow(self):
        from opi.forms.visualizers.flows import MODAL_EDIT_KEYCLOAK_FLOW

        return MODAL_EDIT_KEYCLOAK_FLOW

    @pytest.fixture
    def project_data(self) -> dict[str, Any]:
        return {
            "display-name": "Test Project",
            "services": [
                copy.deepcopy(KEYCLOAK_CONFIG),
                "publish-on-web",
            ],
            "config": {"age-public-key": "age1xxx"},
        }

    def test_split_populates_virtual_key(self, keycloak_flow, project_data):
        """_split_data_across_sections should populate _services-config for keycloak."""
        step_data = _split_data_across_sections(keycloak_flow, project_data)
        assert "keycloak-config" in step_data
        assert "_services-config" in step_data["keycloak-config"]
        assert "keycloak" in step_data["keycloak-config"]["_services-config"]

    def test_merged_data_with_template_services_produces_list(self, keycloak_flow, project_data):
        """get_merged_data must produce services as a list, not a dict.

        When base_data includes the services list, devirtualize should
        merge virtual config back into the list. Without the list in
        base_data, devirtualize falls back to a dict which breaks
        smart_get_value.
        """
        step_data = _split_data_across_sections(keycloak_flow, project_data)
        state = _make_state(
            flow_id="modal-edit-keycloak-config",
            step_data=step_data,
            active_sections=["keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        # Simulate what router_detail_edit does: seed services in base_data
        state.base_data = {"services": copy.deepcopy(project_data["services"])}

        merged = state.get_merged_data()

        assert "services" in merged
        assert isinstance(merged["services"], list), (
            f"services should be a list after devirtualize, got {type(merged['services'])}"
        )

    def test_merged_data_without_template_services_produces_dict(self, keycloak_flow, project_data):
        """Without services in base_data, devirtualize produces a dict.

        This documents the bug that existed before the fix: smart_get_value
        expects a list and returns None for all fields.
        """
        step_data = _split_data_across_sections(keycloak_flow, project_data)
        state = _make_state(
            flow_id="modal-edit-keycloak-config",
            step_data=step_data,
            active_sections=["keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        # No base_data with services — the old buggy path

        merged = state.get_merged_data()

        # Documents the problematic fallback behavior
        if "services" in merged:
            assert isinstance(merged["services"], dict), (
                "Without base_data services, devirtualize should produce a dict (the bug)"
            )

    def test_smart_get_value_works_with_list(self, keycloak_flow, project_data):
        """smart_get_value should find keycloak config when services is a list."""
        from opi.forms.editables.service_path import smart_get_value

        step_data = _split_data_across_sections(keycloak_flow, project_data)
        state = _make_state(
            flow_id="modal-edit-keycloak-config",
            step_data=step_data,
            active_sections=["keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        state.base_data = {"services": copy.deepcopy(project_data["services"])}

        merged = state.get_merged_data()

        # This is what the bridge does to read display values
        template_value = smart_get_value(merged, "services/keycloak/config/template")
        assert template_value == "sso-support"

        restrict_value = smart_get_value(merged, "services/keycloak/config/restrict-access/enabled")
        assert restrict_value is True

    def test_post_save_action_is_process_project(self, keycloak_flow):
        """Keycloak config section should trigger process_project, not save_only."""
        section = keycloak_flow.sections[0]
        assert section.post_save_action == "process_project", (
            f"Expected 'process_project', got '{section.post_save_action}'"
        )

    def test_determine_flow_action_returns_process_project(self, keycloak_flow):
        """_determine_flow_action should return process_project for keycloak edit."""
        from opi.web.router_detail_edit import _determine_flow_action

        # When active_sections contains the keycloak section
        action = _determine_flow_action(keycloak_flow, keycloak_flow.sections)
        assert action == "process_project"

    @pytest.mark.parametrize(
        "existing_entry",
        [
            pytest.param({"keycloak": {"config": {"template": "sso-support"}}}, id="legacy-dict"),
            pytest.param({"name": "keycloak", "config": {"template": "sso-support"}}, id="record"),
            pytest.param("keycloak", id="bare-string"),
        ],
        # A service that already carries config is a dict, not a bare string, in either
        # entry form. Folding used to match on "entry is a bare string", so the second
        # and every later edit was dropped without an error: the modal reported success
        # and the store logged "no change" (toets-hn7 keycloak template, 2026-08-05).
    )
    def test_edit_sticks_for_every_entry_form(self, existing_entry):
        """A changed template must survive the merge whatever shape the entry has."""
        from opi.forms.editables.service_path import smart_get_value

        state = _make_state(
            flow_id="modal-edit-keycloak-config",
            step_data={"keycloak-config": {"_services-config": {"keycloak": {"config": {"template": "sso-only"}}}}},
            active_sections=["keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        state.base_data = {"services": ["publish-on-web", copy.deepcopy(existing_entry)]}

        merged = state.get_merged_data()

        assert smart_get_value(merged, "services/keycloak/config/template") == "sso-only"

    def test_edit_keeps_untouched_config_keys(self, project_data):
        """Folding overlays the edited key; config the step did not carry stays put."""
        from opi.forms.editables.service_path import smart_get_value

        state = _make_state(
            flow_id="modal-edit-keycloak-config",
            step_data={"keycloak-config": {"_services-config": {"keycloak": {"config": {"template": "sso-only"}}}}},
            active_sections=["keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        state.base_data = {"services": copy.deepcopy(project_data["services"])}

        merged = state.get_merged_data()

        assert smart_get_value(merged, "services/keycloak/config/template") == "sso-only"
        assert smart_get_value(merged, "services/keycloak/config/restrict-access/realm-role") == "allowed-user"

    def test_deselected_service_is_not_resurrected(self):
        """A carrier for a service no longer selected must not add it back."""
        state = _make_state(
            flow_id="modal-edit-keycloak-config",
            step_data={"keycloak-config": {"_services-config": {"keycloak": {"config": {"template": "sso-only"}}}}},
            active_sections=["keycloak-config"],
            virt_mappings={"_services-config": "services"},
        )
        state.base_data = {"services": ["publish-on-web"]}

        merged = state.get_merged_data()

        assert merged["services"] == ["publish-on-web"]


class TestStandalonePostgresqlEditFlow:
    """Test the standalone postgresql config edit flow."""

    def test_post_save_action_is_process_project(self):
        from opi.forms.visualizers.flows import MODAL_EDIT_POSTGRESQL_FLOW

        section = MODAL_EDIT_POSTGRESQL_FLOW.sections[0]
        assert section.post_save_action == "process_project"


class TestStandaloneAuthWallEditFlow:
    """Test the standalone auth-wall config edit flow."""

    def test_post_save_action_is_process_project(self):
        from opi.forms.visualizers.flows import MODAL_EDIT_AUTH_WALL_FLOW

        section = MODAL_EDIT_AUTH_WALL_FLOW.sections[0]
        assert section.post_save_action == "process_project"
