"""Tests for wizard HTMX routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.layout import Fieldset
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.wizard.state import WizardState
from opi.web.router_wizard import (
    _build_section_summary,
    _get_section_from_flow,
    _render_step_html,
    _section_has_errors,
)


class TestGetSectionFromFlow:
    def test_finds_existing_section(self):
        section = _get_section_from_flow("create-project", "identity")
        assert section.section_id == "identity"

    def test_raises_404_for_unknown_section(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _get_section_from_flow("create-project", "nonexistent")
        assert exc_info.value.status_code == 404

    def test_raises_for_unknown_flow(self):
        with pytest.raises(KeyError, match="Unknown flow"):
            _get_section_from_flow("nonexistent-flow", "identity")


class TestRenderStepHtml:
    def test_renders_section_with_layout(self):
        editable = EditableVisualizer(
            editable=Editable(yaml_path="name"),
            widget=WidgetType.TEXT,
            label="Naam",
        )
        section = FormSection(
            section_id="test",
            title="Test",
            editables=[editable],
            layout=Fieldset(legend="Test", children=["name"]),
        )
        html = _render_step_html(section, yaml_data={"name": "my-project"})
        assert html  # Non-empty HTML
        assert "my-project" in html or "name" in html

    def test_returns_empty_for_no_layout(self):
        section = FormSection(
            section_id="test",
            title="Test",
            layout=None,
        )
        assert _render_step_html(section, yaml_data={}) == ""


class TestBuildSectionSummary:
    def test_summary_with_simple_fields(self):
        section = FormSection(
            section_id="test",
            title="Test",
            editables=[
                EditableVisualizer(editable=Editable(yaml_path="name"), widget=WidgetType.TEXT, label="Naam"),
                EditableVisualizer(
                    editable=Editable(yaml_path="description"), widget=WidgetType.TEXTAREA, label="Omschrijving"
                ),
            ],
        )
        yaml_data = {"name": "test-project", "description": "A test"}
        html = _build_section_summary(section, yaml_data)
        assert "<dl>" in html
        assert "Naam" in html
        assert "test-project" in html
        assert "Omschrijving" in html

    def test_summary_with_none_value(self):
        section = FormSection(
            section_id="test",
            title="Test",
            editables=[
                EditableVisualizer(editable=Editable(yaml_path="missing"), widget=WidgetType.TEXT, label="Missing"),
            ],
        )
        html = _build_section_summary(section, {})
        assert "Geen gegevens ingevuld" in html

    def test_summary_with_list_value(self):
        section = FormSection(
            section_id="test",
            title="Test",
            editables=[
                EditableVisualizer(
                    editable=Editable(yaml_path="clusters"), widget=WidgetType.CHECKBOX_GROUP, label="Clusters"
                ),
            ],
        )
        html = _build_section_summary(section, {"clusters": ["local", "production"]})
        assert "local, production" in html

    def test_summary_with_bool_value(self):
        section = FormSection(
            section_id="test",
            title="Test",
            editables=[
                EditableVisualizer(editable=Editable(yaml_path="enabled"), widget=WidgetType.CHECKBOX, label="Actief"),
            ],
        )
        html = _build_section_summary(section, {"enabled": True})
        assert "Ja" in html

    def test_summary_renders_sequences(self):
        child = EditableVisualizer(
            editable=Editable(yaml_path="items[*]/name"),
            widget=WidgetType.TEXT,
            label="Naam",
        )
        section = FormSection(
            section_id="test",
            title="Test",
            editables=[
                EditableVisualizer(
                    editable=Editable(yaml_path="items"),
                    widget=WidgetType.SEQUENCE,
                    label="Items",
                    children=[child],
                ),
            ],
        )
        html = _build_section_summary(section, {"items": [{"name": "a"}]})
        assert "Items" in html
        assert "(1)" in html  # item count
        assert "a" in html  # item name used as label

    def test_custom_summary_fn(self):
        section = FormSection(
            section_id="test",
            title="Test",
            summary_fn=lambda data: f"<p>Custom: {data.get('name', 'n/a')}</p>",
        )
        html = _build_section_summary(section, {"name": "proj"})
        assert "Custom: proj" in html


class TestSectionHasErrors:
    """Test matching error paths to section editable paths."""

    def test_matches_simple_path(self):
        assert _section_has_errors({"name", "description"}, {"name": ["Required"]})

    def test_no_match(self):
        assert not _section_has_errors({"name"}, {"email": ["Required"]})

    def test_matches_wildcard_to_concrete(self):
        paths = {"users[*]/email", "users[*]/role"}
        errors = {"users[0]/email": ["Required"]}
        assert _section_has_errors(paths, errors)

    def test_matches_multiple_indices(self):
        paths = {"items[*]/name"}
        errors = {"items[2]/name": ["Required"]}
        assert _section_has_errors(paths, errors)


class TestRenderStepHtmlWithRealSections:
    """Test rendering with the actual wizard section definitions."""

    def test_identity_section_renders_all_fields(self):
        from opi.forms.visualizers.wizard_sections import IDENTITY_SECTION

        html = _render_step_html(IDENTITY_SECTION, yaml_data={})
        assert html, "Identity section should produce non-empty HTML"
        # Should contain ROOS component tags (pre-processing)
        assert "display-name" in html
        assert "description" in html
        assert "clusters" in html

    def test_identity_section_renders_with_data(self):
        from opi.forms.visualizers.wizard_sections import IDENTITY_SECTION

        data = {
            "name": "test-proj",
            "display-name": "Test Project",
            "description": "A description",
            "clusters": ["local"],
        }
        html = _render_step_html(IDENTITY_SECTION, yaml_data=data)
        assert "Test Project" in html
        assert "A description" in html

    def test_services_section_renders(self):
        from opi.forms.visualizers.wizard_sections import SERVICES_SECTION

        html = _render_step_html(SERVICES_SECTION, yaml_data={})
        assert html, "Services section should produce non-empty HTML"

    def test_team_section_renders(self):
        from opi.forms.visualizers.wizard_sections import TEAM_SECTION

        html = _render_step_html(TEAM_SECTION, yaml_data={})
        assert html, "Team section should produce non-empty HTML"

    def test_components_section_renders(self):
        from opi.forms.visualizers.wizard_sections import COMPONENTS_SECTION

        html = _render_step_html(COMPONENTS_SECTION, yaml_data={})
        assert html, "Components section should produce non-empty HTML"


class TestTemplateProcessComponents:
    """Verify templates use process_components filter for form HTML."""

    TEMPLATES_DIR = Path(__file__).parent.parent.parent / "opi" / "templates" / "wizard"

    def test_wizard_step_uses_process_components(self):
        content = (self.TEMPLATES_DIR / "wizard_step.html.j2").read_text()
        assert "process_components" in content, (
            "wizard_step.html.j2 must use the process_components filter, not | safe, for step_html rendering"
        )
        assert "step_html | safe" not in content, "wizard_step.html.j2 should NOT use | safe for step_html"

    def test_wizard_step_oob_swap_is_conditional(self):
        """OOB swap for step indicator should be conditional to avoid duplication."""
        content = (self.TEMPLATES_DIR / "wizard_step.html.j2").read_text()
        assert "if not embedded" in content, (
            "OOB swap should be conditional on 'embedded' to avoid duplicate step indicators on initial page load"
        )

    def test_wizard_page_includes_step_with_embedded(self):
        """Full page should include step template with embedded=True."""
        content = (self.TEMPLATES_DIR / "wizard_page.html.j2").read_text()
        assert "embedded=True" in content, (
            "wizard_page.html.j2 should set embedded=True when including wizard_step.html.j2 to suppress OOB swap"
        )


class TestWizardStateIntegration:
    """Integration tests for wizard state with real flows."""

    def test_create_flow_has_identity_first(self):
        from opi.forms.visualizers.flows import CREATE_FLOW

        assert CREATE_FLOW.sections[0].section_id == "identity"

    def test_wizard_state_with_create_flow(self):
        from opi.forms.visualizers.flows import CREATE_FLOW
        from opi.forms.wizard.resolver import (
            resolve_active_sections,
        )

        state = WizardState(
            flow_id="create-project",
            current_step="identity",
            active_sections=[s.section_id for s in CREATE_FLOW.sections],
        )
        # With no services selected, conditional sections should be hidden
        active = resolve_active_sections(CREATE_FLOW, state.step_data)
        active_ids = [s.section_id for s in active]
        assert "identity" in active_ids
        assert "services" in active_ids
        assert "keycloak-config" not in active_ids

    def test_wizard_state_with_services_selected(self):
        from opi.forms.visualizers.flows import CREATE_FLOW
        from opi.forms.wizard.resolver import resolve_active_sections

        state = WizardState(
            flow_id="create-project",
            current_step="services",
            step_data={"services": {"services": ["keycloak"]}},
            active_sections=[s.section_id for s in CREATE_FLOW.sections],
        )
        active = resolve_active_sections(CREATE_FLOW, state.step_data)
        active_ids = [s.section_id for s in active]
        assert "keycloak-config" in active_ids
        assert "postgresql-config" not in active_ids
