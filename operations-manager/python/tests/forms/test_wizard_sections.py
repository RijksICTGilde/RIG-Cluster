"""Tests for wizard section definitions and conditional visibility."""

from __future__ import annotations

import pytest
from opi.forms.visualizers.flows import CREATE_FLOW, EDIT_FLOW, FLOW_REGISTRY, get_flow
from opi.forms.visualizers.wizard_sections import (
    ALL_SECTIONS,
    AUTH_WALL_CONFIG_SECTION,
    COMPONENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
    DEPLOYMENTS_SECTION,
    IDENTITY_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    SERVICE_CONFIG_SECTIONS,
    SERVICES_EDIT_SECTION,
    SERVICES_SECTION,
    TEAM_SECTION,
    _strip_removed_services_from_components,
)
from opi.services.services import service_entry_name


class TestSectionDefinitions:
    def test_all_sections_have_section_id(self):
        for section in ALL_SECTIONS:
            assert section.section_id, f"Section missing section_id: {section.title}"

    def test_all_sections_have_title(self):
        for section in ALL_SECTIONS:
            assert section.title, f"Section missing title: {section.section_id}"

    def test_all_sections_have_editables(self):
        for section in ALL_SECTIONS:
            assert len(section.editables) > 0, f"Section {section.section_id} has no editables"

    def test_all_sections_have_layout(self):
        for section in ALL_SECTIONS:
            assert section.layout is not None, f"Section {section.section_id} has no layout"

    def test_section_ids_are_unique(self):
        ids = [s.section_id for s in ALL_SECTIONS]
        assert len(ids) == len(set(ids)), f"Duplicate section IDs: {ids}"

    def test_total_section_count(self):
        assert len(ALL_SECTIONS) == 11


class TestCoreSections:
    def test_identity_section(self):
        assert IDENTITY_SECTION.section_id == "identity"
        assert len(IDENTITY_SECTION.editables) == 3
        assert IDENTITY_SECTION.visible is True

    def test_services_section(self):
        assert SERVICES_SECTION.section_id == "services"
        assert len(SERVICES_SECTION.editables) == 1
        assert SERVICES_SECTION.visible is True

    def test_team_section(self):
        assert TEAM_SECTION.section_id == "team"
        assert len(TEAM_SECTION.editables) == 1

    def test_components_section(self):
        assert COMPONENTS_SECTION.section_id == "components"
        assert len(COMPONENTS_SECTION.editables) == 1

    def test_deployments_section(self):
        assert DEPLOYMENTS_SECTION.section_id == "deployments"
        assert len(DEPLOYMENTS_SECTION.editables) == 1

    def test_config_display_section_is_readonly(self):
        assert CONFIG_DISPLAY_SECTION.section_id == "config"
        assert CONFIG_DISPLAY_SECTION.is_readonly is True
        assert len(CONFIG_DISPLAY_SECTION.editables) == 3


class TestConditionalSections:
    def test_keycloak_visible_when_selected(self):
        assert callable(KEYCLOAK_CONFIG_SECTION.visible)
        assert KEYCLOAK_CONFIG_SECTION.visible({"services": ["keycloak"]})

    def test_keycloak_hidden_when_not_selected(self):
        assert not KEYCLOAK_CONFIG_SECTION.visible({"services": ["redis"]})
        assert not KEYCLOAK_CONFIG_SECTION.visible({})
        assert not KEYCLOAK_CONFIG_SECTION.visible({"services": []})

    def test_postgresql_visible_when_selected(self):
        assert callable(POSTGRESQL_CONFIG_SECTION.visible)
        assert POSTGRESQL_CONFIG_SECTION.visible({"services": ["namespace-postgresql-database"]})

    def test_postgresql_hidden_when_not_selected(self):
        assert not POSTGRESQL_CONFIG_SECTION.visible({"services": ["keycloak"]})

    def test_auth_wall_visible_when_selected(self):
        assert callable(AUTH_WALL_CONFIG_SECTION.visible)
        assert AUTH_WALL_CONFIG_SECTION.visible({"services": ["authorization-wall"]})

    def test_auth_wall_hidden_when_not_selected(self):
        assert not AUTH_WALL_CONFIG_SECTION.visible({"services": []})

    def test_service_with_dict_format(self):
        """Services can also be a list of dicts with 'name' key."""
        data = {"services": [{"name": "keycloak"}, {"name": "redis"}]}
        assert KEYCLOAK_CONFIG_SECTION.visible(data)
        assert not POSTGRESQL_CONFIG_SECTION.visible(data)


class TestServiceConfigSectionsLookup:
    def test_keycloak_in_lookup(self):
        assert "keycloak" in SERVICE_CONFIG_SECTIONS
        assert SERVICE_CONFIG_SECTIONS["keycloak"] is KEYCLOAK_CONFIG_SECTION

    def test_postgresql_in_lookup(self):
        assert "namespace-postgresql-database" in SERVICE_CONFIG_SECTIONS

    def test_auth_wall_in_lookup(self):
        assert "authorization-wall" in SERVICE_CONFIG_SECTIONS

    def test_sleep_mode_present(self):
        assert "sleep-mode" in SERVICE_CONFIG_SECTIONS

    def test_lookup_count(self):
        assert len(SERVICE_CONFIG_SECTIONS) == 6


class TestFlowDefinitions:
    def test_create_flow(self):
        assert CREATE_FLOW.flow_id == "create-project"
        assert CREATE_FLOW.show_review is True
        assert len(CREATE_FLOW.sections) == 13
        assert "attachments" in [s.section_id for s in CREATE_FLOW.sections]
        # invite-config sits after the keycloak step so its realm-role picker reads the
        # keycloak config already entered in the draft.
        create_ids = [s.section_id for s in CREATE_FLOW.sections]
        assert create_ids.index("invite-config") > create_ids.index("keycloak-config")
        # sleep-mode-config sits after the components step so its waker-component select
        # is populated from the components already in the draft project.
        section_ids = [s.section_id for s in CREATE_FLOW.sections]
        assert section_ids.index("sleep-mode-config") > section_ids.index("components")

    def test_edit_flow(self):
        assert EDIT_FLOW.flow_id == "edit-project"
        assert EDIT_FLOW.show_review is False
        assert EDIT_FLOW.save_per_section is True
        assert len(EDIT_FLOW.sections) == 12
        # Attachments are edited via a modal/service-edit flow in edit mode,
        # so the edit wizard has no dedicated attachments section (unlike create).
        assert "attachments" not in [s.section_id for s in EDIT_FLOW.sections]

    def test_create_flow_does_not_include_deployments(self):
        section_ids = [s.section_id for s in CREATE_FLOW.sections]
        assert "deployments" not in section_ids

    def test_edit_flow_includes_deployments_and_config(self):
        section_ids = [s.section_id for s in EDIT_FLOW.sections]
        assert "deployments" in section_ids
        assert "config" in section_ids

    def test_conditional_sections_in_both_flows(self):
        for flow in [CREATE_FLOW, EDIT_FLOW]:
            section_ids = [s.section_id for s in flow.sections]
            assert "keycloak-config" in section_ids
            assert "postgresql-config" in section_ids
            assert "auth-wall-config" in section_ids


class TestFlowRegistry:
    def test_registry_has_both_flows(self):
        assert "create-project" in FLOW_REGISTRY
        assert "edit-project" in FLOW_REGISTRY

    def test_get_flow_returns_correct_flow(self):
        assert get_flow("create-project") is CREATE_FLOW
        assert get_flow("edit-project") is EDIT_FLOW

    def test_get_flow_raises_on_unknown(self):
        with pytest.raises(KeyError, match="Unknown flow"):
            get_flow("nonexistent")


class TestServiceEntryName:
    """The component-service reconciliation now uses the canonical helper (which also
    resolves the ``{reference: X}`` record the old local reader ignored)."""

    def test_string_entry(self):
        assert service_entry_name("keycloak") == "keycloak"

    def test_dict_with_name(self):
        assert service_entry_name({"name": "keycloak"}) == "keycloak"

    def test_dict_with_reference(self):
        assert service_entry_name({"reference": "persistent-storage", "config": []}) == "persistent-storage"

    def test_single_key_dict(self):
        assert service_entry_name({"persistent-storage": {"config": []}}) == "persistent-storage"

    def test_none_for_invalid(self):
        assert service_entry_name(42) is None

    def test_none_for_multi_key_dict(self):
        assert service_entry_name({"a": 1, "b": 2}) is None


class TestStripRemovedServicesFromComponents:
    def test_removes_string_service_from_component(self):
        data = {
            "services": ["persistent-storage"],
            "components": [
                {"name": "app", "services": ["keycloak", "persistent-storage"]},
            ],
        }
        _strip_removed_services_from_components(data, {})
        assert data["components"][0]["services"] == ["persistent-storage"]

    def test_removes_dict_service_from_component(self):
        data = {
            "services": ["keycloak"],
            "components": [
                {
                    "name": "app",
                    "services": [
                        "keycloak",
                        {"persistent-storage": {"config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}]}},
                    ],
                },
            ],
        }
        _strip_removed_services_from_components(data, {})
        assert data["components"][0]["services"] == ["keycloak"]

    def test_keeps_all_when_nothing_removed(self):
        data = {
            "services": ["keycloak", "persistent-storage"],
            "components": [
                {"name": "app", "services": ["keycloak", "persistent-storage"]},
            ],
        }
        _strip_removed_services_from_components(data, {})
        assert data["components"][0]["services"] == ["keycloak", "persistent-storage"]

    def test_handles_no_components(self):
        data = {"services": ["keycloak"]}
        _strip_removed_services_from_components(data, {})
        assert "components" not in data

    def test_handles_component_without_services(self):
        data = {
            "services": [],
            "components": [{"name": "app"}],
        }
        _strip_removed_services_from_components(data, {})
        assert "services" not in data["components"][0]

    def test_cleans_multiple_components(self):
        data = {
            "services": ["persistent-storage"],
            "components": [
                {"name": "frontend", "services": ["keycloak", "persistent-storage"]},
                {"name": "backend", "services": ["keycloak", "namespace-postgresql-database"]},
            ],
        }
        _strip_removed_services_from_components(data, {})
        assert data["components"][0]["services"] == ["persistent-storage"]
        assert data["components"][1]["services"] == []

    def test_services_edit_section_has_post_merge(self):
        assert SERVICES_EDIT_SECTION.post_merge is _strip_removed_services_from_components
