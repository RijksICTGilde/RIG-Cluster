"""Tests for wizard state management, session integration, and step resolver."""

from __future__ import annotations

from unittest.mock import MagicMock

from opi.forms.visualizers.flows import FlowMode, FormFlow
from opi.forms.visualizers.sections import FormSection
from opi.forms.wizard.resolver import (
    get_section_metadata,
    resolve_active_section_ids,
    resolve_active_sections,
)
from opi.forms.wizard.session import (
    clear_wizard_state,
    get_wizard_state,
    init_wizard_state,
    save_wizard_state,
)
from opi.forms.wizard.state import CLEARED_FIELD, WizardState, WizardSteps

# ---------------------------------------------------------------------------
# WizardSteps tests
# ---------------------------------------------------------------------------


class TestWizardSteps:
    def _make_steps(self, current: str = "services", completed: list[str] | None = None) -> WizardSteps:
        return WizardSteps(
            current=current,
            all=["identity", "services", "team", "components"],
            titles={
                "identity": "Project",
                "services": "Services",
                "team": "Team",
                "components": "Componenten",
            },
            icons={
                "identity": "document",
                "services": "verwerking",
                "team": "mens",
                "components": "zaak",
            },
            completed=completed or [],
        )

    def test_count(self):
        steps = self._make_steps()
        assert steps.count == 4

    def test_index(self):
        steps = self._make_steps(current="services")
        assert steps.index == 1

    def test_index_first(self):
        steps = self._make_steps(current="identity")
        assert steps.index == 0

    def test_index_unknown_defaults_to_zero(self):
        steps = self._make_steps(current="nonexistent")
        assert steps.index == 0

    def test_first_and_last(self):
        steps = self._make_steps()
        assert steps.first == "identity"
        assert steps.last == "components"

    def test_prev_and_next(self):
        steps = self._make_steps(current="services")
        assert steps.prev == "identity"
        assert steps.next == "team"

    def test_prev_none_at_first(self):
        steps = self._make_steps(current="identity")
        assert steps.prev is None

    def test_next_none_at_last(self):
        steps = self._make_steps(current="components")
        assert steps.next is None

    def test_is_first_and_is_last(self):
        assert self._make_steps(current="identity").is_first
        assert not self._make_steps(current="identity").is_last
        assert self._make_steps(current="components").is_last
        assert not self._make_steps(current="components").is_first

    def test_progress_pct_empty(self):
        steps = self._make_steps(completed=[])
        assert steps.progress_pct == 0

    def test_progress_pct_half(self):
        steps = self._make_steps(completed=["identity", "services"])
        assert steps.progress_pct == 50

    def test_progress_pct_full(self):
        steps = self._make_steps(completed=["identity", "services", "team", "components"])
        assert steps.progress_pct == 100


# ---------------------------------------------------------------------------
# WizardState tests
# ---------------------------------------------------------------------------


class TestWizardState:
    def test_initial_state(self):
        state = WizardState(
            flow_id="create-project",
            current_step="identity",
            active_sections=["identity", "services"],
        )
        assert state.flow_id == "create-project"
        assert state.current_step == "identity"
        assert state.completed_steps == []
        assert state.step_data == {}
        assert state.project_name is None

    def test_mark_completed(self):
        state = WizardState(flow_id="test", current_step="a", active_sections=["a", "b"])
        state.mark_completed("a")
        assert "a" in state.completed_steps
        # Idempotent
        state.mark_completed("a")
        assert state.completed_steps.count("a") == 1

    def test_store_step_data(self):
        state = WizardState(flow_id="test", current_step="a", active_sections=["a"])
        state.store_step_data("a", {"name": "test-project"})
        assert state.step_data["a"] == {"name": "test-project"}

    def test_get_merged_data(self):
        state = WizardState(
            flow_id="test",
            current_step="b",
            active_sections=["a", "b"],
            step_data={
                "a": {"name": "proj", "clusters": ["local"]},
                "b": {"services": ["keycloak"]},
            },
        )
        merged = state.get_merged_data()
        assert merged == {
            "name": "proj",
            "clusters": ["local"],
            "services": ["keycloak"],
        }

    def test_get_merged_data_respects_order(self):
        """Later sections override earlier ones on key collision."""
        state = WizardState(
            flow_id="test",
            current_step="b",
            active_sections=["a", "b"],
            step_data={
                "a": {"key": "from_a"},
                "b": {"key": "from_b"},
            },
        )
        assert state.get_merged_data()["key"] == "from_b"

    def test_get_merged_data_cleared_field_removes_template_value(self):
        """A CLEARED_FIELD tombstone deletes the snapshot value instead of resurrecting it."""
        state = WizardState(
            flow_id="test",
            current_step="a",
            active_sections=["a"],
            template_data={
                "components": [{"name": "web", "aliases": {"DATABASE_URL": "old"}, "image": "x:1"}],
            },
            step_data={
                "a": {"components": [{"name": "web", "aliases": CLEARED_FIELD}]},
            },
        )
        merged = state.get_merged_data()
        assert "aliases" not in merged["components"][0]
        assert merged["components"][0]["image"] == "x:1"

    def test_get_merged_data_cleared_field_swept_on_wholesale_replace(self):
        """Tombstones never leak into merged output, even without a template value."""
        state = WizardState(
            flow_id="test",
            current_step="a",
            active_sections=["a"],
            step_data={
                "a": {"components": [{"name": "web", "aliases": CLEARED_FIELD}]},
            },
        )
        merged = state.get_merged_data()
        assert merged["components"] == [{"name": "web"}]

    def test_get_merged_data_skips_inactive(self):
        """Only data from active_sections is included."""
        state = WizardState(
            flow_id="test",
            current_step="a",
            active_sections=["a"],
            step_data={
                "a": {"name": "proj"},
                "removed": {"stale": "data"},
            },
        )
        merged = state.get_merged_data()
        assert "stale" not in merged

    def test_serialization_roundtrip(self):
        state = WizardState(
            flow_id="create-project",
            current_step="services",
            completed_steps=["identity"],
            step_data={"identity": {"name": "proj"}},
            active_sections=["identity", "services", "team"],
            project_name=None,
        )
        data = state.to_dict()
        restored = WizardState.from_dict(data)
        assert restored.flow_id == state.flow_id
        assert restored.current_step == state.current_step
        assert restored.completed_steps == state.completed_steps
        assert restored.step_data == state.step_data
        assert restored.active_sections == state.active_sections
        assert restored.project_name == state.project_name

    def test_stash_inactive_sections(self):
        """Deactivated section data moves to stashed_data."""
        state = WizardState(
            flow_id="test",
            current_step="services",
            completed_steps=["identity", "services", "keycloak-config"],
            active_sections=["identity", "services", "keycloak-config", "team"],
            step_data={
                "identity": {"display-name": "My Project"},
                "services": {"services": ["keycloak"]},
                "keycloak-config": {"services": [{"keycloak": {"config": {"template": "sso-only"}}}]},
            },
        )
        # Keycloak deselected - keycloak-config is no longer active
        new_active = ["identity", "services", "team"]
        state.stash_inactive_sections(new_active)

        assert "keycloak-config" not in state.step_data
        assert "keycloak-config" in state.stashed_data
        assert state.stashed_data["keycloak-config"]["services"][0]["keycloak"]["config"]["template"] == "sso-only"
        assert "keycloak-config" not in state.completed_steps
        # Other sections untouched
        assert "identity" in state.step_data
        assert "services" in state.step_data

    def test_restore_from_stash(self):
        """Re-selecting a service restores config from stash."""
        state = WizardState(
            flow_id="test",
            current_step="services",
            completed_steps=["identity", "services"],
            active_sections=["identity", "services", "team"],
            step_data={
                "identity": {"display-name": "My Project"},
                "services": {"services": ["keycloak"]},
            },
            stashed_data={
                "keycloak-config": {"services": [{"keycloak": {"config": {"template": "sso-only"}}}]},
            },
        )
        # Keycloak re-selected - keycloak-config becomes active again
        new_active = ["identity", "services", "keycloak-config", "team"]
        state.stash_inactive_sections(new_active)

        assert "keycloak-config" in state.step_data
        assert "keycloak-config" not in state.stashed_data
        assert state.step_data["keycloak-config"]["services"][0]["keycloak"]["config"]["template"] == "sso-only"

    def test_stash_roundtrip_serialization(self):
        """Stashed data survives serialization roundtrip."""
        state = WizardState(
            flow_id="test",
            current_step="services",
            active_sections=["identity", "services"],
            stashed_data={"keycloak-config": {"services": [{"keycloak": {"config": {}}}]}},
        )
        restored = WizardState.from_dict(state.to_dict())
        assert restored.stashed_data == state.stashed_data

    def test_get_steps(self):
        state = WizardState(
            flow_id="test",
            current_step="services",
            completed_steps=["identity"],
            active_sections=["identity", "services", "team"],
        )
        sections_meta = {
            "identity": ("Project", "document"),
            "services": ("Services", "verwerking"),
            "team": ("Team", "mens"),
        }
        steps = state.get_steps(sections_meta)
        assert steps.current == "services"
        assert steps.count == 3
        assert steps.titles["identity"] == "Project"
        assert steps.prev == "identity"
        assert steps.next == "team"
        assert steps.completed == ["identity"]


# ---------------------------------------------------------------------------
# Session integration tests
# ---------------------------------------------------------------------------


class TestSessionIntegration:
    def _make_request(self, session: dict | None = None) -> MagicMock:
        request = MagicMock()
        request.session = session if session is not None else {}
        return request

    def test_get_wizard_state_empty_session(self):
        request = self._make_request()
        assert get_wizard_state(request) is None

    def test_save_and_get_wizard_state(self):
        request = self._make_request()
        state = WizardState(
            flow_id="create-project",
            current_step="identity",
            active_sections=["identity", "services"],
        )
        save_wizard_state(request, state)
        restored = get_wizard_state(request)
        assert restored is not None
        assert restored.flow_id == "create-project"
        assert restored.current_step == "identity"

    def test_clear_wizard_state(self):
        request = self._make_request()
        state = WizardState(flow_id="test", current_step="a", active_sections=["a"])
        save_wizard_state(request, state)
        clear_wizard_state(request)
        assert get_wizard_state(request) is None

    def test_clear_wizard_state_noop_on_empty(self):
        request = self._make_request()
        clear_wizard_state(request)  # Should not raise

    def test_invalid_session_data_returns_none(self):
        request = self._make_request({"wizard_state": "invalid"})
        assert get_wizard_state(request) is None

    def test_init_wizard_state(self):
        request = self._make_request()
        state = init_wizard_state(
            request,
            flow_id="create-project",
            first_step="identity",
            active_sections=["identity", "services", "team"],
        )
        assert state.flow_id == "create-project"
        assert state.current_step == "identity"
        # Verify it was saved to session
        assert get_wizard_state(request) is not None

    def test_init_wizard_state_with_project_name(self):
        request = self._make_request()
        state = init_wizard_state(
            request,
            flow_id="edit-project",
            first_step="identity",
            active_sections=["identity"],
            project_name="my-project",
        )
        assert state.project_name == "my-project"


# ---------------------------------------------------------------------------
# Resolver tests
# ---------------------------------------------------------------------------


class TestResolver:
    def _make_section(self, section_id: str, visible: bool | callable = True) -> FormSection:
        return FormSection(section_id=section_id, title=section_id.title(), visible=visible)

    def _make_flow(self, sections: list[FormSection]) -> FormFlow:
        return FormFlow(
            flow_id="test",
            title="Test",
            mode=FlowMode.WIZARD,
            sections=sections,
        )

    def test_all_visible(self):
        flow = self._make_flow(
            [
                self._make_section("a"),
                self._make_section("b"),
            ]
        )
        active = resolve_active_sections(flow, {})
        assert len(active) == 2

    def test_conditional_section_hidden(self):
        flow = self._make_flow(
            [
                self._make_section("a"),
                self._make_section("b", visible=lambda data: "x" in data),
                self._make_section("c"),
            ]
        )
        active = resolve_active_sections(flow, {})
        assert [s.section_id for s in active] == ["a", "c"]

    def test_conditional_section_shown(self):
        flow = self._make_flow(
            [
                self._make_section("a"),
                self._make_section("b", visible=lambda data: "x" in data),
                self._make_section("c"),
            ]
        )
        active = resolve_active_sections(flow, {"a": {"x": True}})
        assert [s.section_id for s in active] == ["a", "b", "c"]

    def test_resolve_active_section_ids(self):
        flow = self._make_flow(
            [
                self._make_section("a"),
                self._make_section("b", visible=False),
            ]
        )
        ids = resolve_active_section_ids(flow, {})
        assert ids == ["a"]

    def test_get_section_metadata(self):
        sections = [
            FormSection(section_id="a", title="Alpha", icon="star"),
            FormSection(section_id="b", title="Beta", icon=None),
        ]
        meta = get_section_metadata(sections)
        assert meta == {
            "a": ("Alpha", "star"),
            "b": ("Beta", None),
        }

    def test_resolver_merges_step_data(self):
        """Resolver merges step_data before evaluating conditions."""
        flow = self._make_flow(
            [
                self._make_section("services"),
                self._make_section(
                    "keycloak",
                    visible=lambda data: "keycloak" in data.get("services", []),
                ),
            ]
        )
        # Keycloak selected in services step
        step_data = {"services": {"services": ["keycloak", "redis"]}}
        active = resolve_active_section_ids(flow, step_data)
        assert "keycloak" in active

        # No keycloak
        step_data = {"services": {"services": ["redis"]}}
        active = resolve_active_section_ids(flow, step_data)
        assert "keycloak" not in active

    def test_services_section_wins_on_key_collision(self):
        """When keycloak-config step_data also has a 'services' key,
        the services section's value must take priority."""
        flow = self._make_flow(
            [
                self._make_section("services"),
                self._make_section(
                    "keycloak-config",
                    visible=lambda data: "keycloak" in data.get("services", []),
                ),
            ]
        )
        # Services section says keycloak is deselected, but keycloak-config's
        # stale data still has a services list that includes keycloak.
        step_data = {
            "services": {"services": ["publish-on-web"]},
            "keycloak-config": {
                "services": [
                    {"keycloak": {"config": {"template": "sso-only"}}},
                    "publish-on-web",
                ],
            },
        }
        active = resolve_active_section_ids(flow, step_data)
        assert "keycloak-config" not in active
