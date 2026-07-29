"""Tests for project editable registry definitions and layout."""

from __future__ import annotations

from opi.forms.layout import ButtonGroup, Fieldset, Sequence
from opi.forms.visualizers.project_registry import (
    AGE_PRIVATE_KEY,
    AGE_PUBLIC_KEY,
    API_KEY,
    CLUSTERS,
    COMPONENTS_SEQUENCE,
    DEPLOYMENTS_SEQUENCE,
    DESCRIPTION,
    DISPLAY_NAME,
    SERVICES,
    USERS_SEQUENCE,
    get_all_project_editables,
    get_project_form_layout,
)


class TestEditableDefinitions:
    def test_get_all_project_editables_returns_expected_count(self):
        editables = get_all_project_editables()
        assert len(editables) == 10

    def test_all_editables_have_yaml_path(self):
        for editable in get_all_project_editables():
            assert editable.editable.yaml_path, f"Editable {editable.label} missing yaml_path"

    def test_all_editables_have_widget(self):
        for editable in get_all_project_editables():
            assert editable.widget, f"Editable {editable.label} missing widget"

    def test_all_editables_have_label(self):
        for editable in get_all_project_editables():
            assert editable.label, f"Editable at {editable.editable.yaml_path} missing label"

    def test_display_name_editable(self):
        assert DISPLAY_NAME.editable.required is True
        assert DISPLAY_NAME.editable.validator is not None

    def test_description_editable(self):
        assert str(DESCRIPTION.widget) == "textarea"
        assert DESCRIPTION.editable.required is True

    def test_clusters_is_select_with_converter(self):
        assert str(CLUSTERS.widget) == "select"
        assert CLUSTERS.editable.values_provider == "ClusterOptionsProvider"
        assert CLUSTERS.editable.required is True
        assert CLUSTERS.editable.converter is not None
        assert CLUSTERS.editable.default is not None

    def test_services_has_converter(self):
        assert SERVICES.editable.converter is not None
        assert SERVICES.editable.values_provider == "ServiceOptionsProvider"


class TestSequenceEditables:
    def test_users_sequence_has_children(self):
        assert str(USERS_SEQUENCE.widget) == "sequence"
        assert USERS_SEQUENCE.children is not None
        assert len(USERS_SEQUENCE.children) == 2
        assert USERS_SEQUENCE.editable.min_items == 1

    def test_users_children_paths(self):
        children = USERS_SEQUENCE.children or []
        paths = [c.editable.yaml_path for c in children]
        assert "users[*]/email" in paths
        assert "users[*]/role" in paths

    def test_components_sequence_has_children(self):
        assert str(COMPONENTS_SEQUENCE.widget) == "sequence"
        assert COMPONENTS_SEQUENCE.children is not None
        # +4 over the base for the health-check service's component fields
        # (scheme, port, liveness-path, readiness-path).
        assert len(COMPONENTS_SEQUENCE.children) == 23
        assert COMPONENTS_SEQUENCE.editable.min_items == 1

    def test_deployments_sequence_has_children(self):
        assert str(DEPLOYMENTS_SEQUENCE.widget) == "sequence"
        assert DEPLOYMENTS_SEQUENCE.children is not None
        assert len(DEPLOYMENTS_SEQUENCE.children) == 15

    def test_deployments_has_nested_components_sequence(self):
        children = DEPLOYMENTS_SEQUENCE.children or []
        nested_seq = [c for c in children if str(c.widget) == "sequence"]
        assert len(nested_seq) == 1
        assert nested_seq[0].editable.yaml_path == "deployments[*]/components"
        assert nested_seq[0].children is not None
        # reference, image, pullPolicy, user-env-vars, per-deployment attachments sequence
        assert len(nested_seq[0].children) == 5


class TestReadonlyEditables:
    def test_age_public_key_is_readonly(self):
        assert AGE_PUBLIC_KEY.readonly is True
        assert str(AGE_PUBLIC_KEY.widget) == "display_card"
        assert AGE_PUBLIC_KEY.editable.converter is not None

    def test_age_private_key_is_readonly(self):
        assert AGE_PRIVATE_KEY.readonly is True
        assert AGE_PRIVATE_KEY.editable.converter is not None

    def test_api_key_is_readonly(self):
        assert API_KEY.readonly is True
        assert API_KEY.editable.converter is not None


class TestFormLayout:
    def test_layout_returns_list(self):
        layout = get_project_form_layout()
        assert isinstance(layout, list)
        assert len(layout) > 0

    def test_layout_has_fieldsets(self):
        layout = get_project_form_layout()
        fieldsets = [e for e in layout if isinstance(e, Fieldset)]
        assert len(fieldsets) == 6
        legends = [f.legend for f in fieldsets]
        assert "Projectgegevens" in legends
        assert "Projectleden" in legends
        assert "Services" in legends
        assert "Componenten" in legends
        assert "Deployments" in legends
        assert "Configuratie" in legends

    def test_layout_has_button_group(self):
        layout = get_project_form_layout()
        buttons = [e for e in layout if isinstance(e, ButtonGroup)]
        assert len(buttons) == 1

    def test_layout_sequences_reference_valid_fields(self):
        layout = get_project_form_layout()
        editables = get_all_project_editables()
        editable_paths = {e.editable.yaml_path for e in editables}

        for element in layout:
            if isinstance(element, Fieldset):
                for child in element.children:
                    if isinstance(child, Sequence):
                        assert child.field_name in editable_paths, (
                            f"Sequence references unknown field: {child.field_name}"
                        )
