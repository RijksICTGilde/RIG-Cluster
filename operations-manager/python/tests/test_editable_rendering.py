"""Tests for editable-driven form rendering."""

from __future__ import annotations

from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.project_registry import get_all_project_editables, get_project_form_layout
from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.widgets.roos import ROOSWidgetAdapter

SAMPLE_YAML = {
    "name": "test-project",
    "display-name": "Test Project",
    "description": "Een test project",
    "clusters": ["local"],
    "users": [
        {"email": "admin@test.nl", "role": "admin"},
        {"email": "dev@test.nl", "role": "developer"},
    ],
    "services": ["publish-on-web", "keycloak"],
    "components": [
        {
            "name": "frontend",
            "type": "single",
            "ports": {"inbound": [8080], "outbound": [443]},
            "resources": {"cpu": "1", "memory": "256Mi"},
            "uses-services": ["publish-on-web"],
            "aliases": {"APP_NAME": "frontend"},
        },
    ],
    "deployments": [
        {
            "name": "production",
            "cluster": "local",
            "repository": "main-repo",
            "subdomain": "app",
            "components": [
                {"reference": "frontend", "image": "nginx:latest", "imagePullPolicy": "Always"},
            ],
        },
    ],
    "config": {
        "age-public-key": "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p",
        "age-private-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----",
        "api-key": "-----BEGIN AGE ENCRYPTED FILE-----\nsecret\n-----END AGE ENCRYPTED FILE-----",
    },
}


def _create_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


class TestRenderFromEditables:
    def test_produces_html(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        layout = get_project_form_layout()
        html = renderer.render_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            layout=layout,
            edit_mode=True,
            action="/projects/edit/test-project",
        )
        assert isinstance(html, str)
        assert len(html) > 0
        assert "<form" in html

    def test_contains_field_values(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        layout = get_project_form_layout()
        html = renderer.render_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            layout=layout,
            edit_mode=True,
        )
        assert "test-project" in html
        assert "Test Project" in html

    def test_render_fields_from_editables_no_form_wrapper(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        layout = get_project_form_layout()
        html = renderer.render_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            layout=layout,
            edit_mode=True,
        )
        assert "<form" not in html
        assert "test-project" in html


class TestBuildFieldsFromEditables:
    def test_builds_fields_dict(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            edit_mode=True,
        )
        assert isinstance(fields, dict)
        assert "name" in fields
        assert "display-name" in fields
        assert "description" in fields

    def test_readonly_on_edit_applied(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            edit_mode=True,
        )
        name_field = fields["name"]
        assert name_field.readonly is True

    def test_readonly_not_applied_on_create(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            edit_mode=False,
        )
        name_field = fields["name"]
        assert name_field.readonly is False


class TestSequenceRendering:
    def test_users_sequence_field(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        users_field = fields["users"]
        assert users_field.widget_type == "sequence"
        assert len(users_field.children) == 2  # 2 users in sample data

    def test_users_sequence_item_children(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        users_field = fields["users"]
        first_item = users_field.children[0]
        assert first_item.widget_type == "sequence_item"
        assert len(first_item.children) == 2  # email + role
        child_names = [c.name for c in first_item.children]
        assert "users[0]/email" in child_names
        assert "users[0]/role" in child_names

    def test_components_sequence_field(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        comp_field = fields["components"]
        assert comp_field.widget_type == "sequence"
        assert len(comp_field.children) == 1  # 1 component

    def test_components_item_has_all_children(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        comp_field = fields["components"]
        first_item = comp_field.children[0]
        assert len(first_item.children) == 8  # 8 child editables


class TestNestedSequenceRendering:
    def test_deployments_sequence_field(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        dep_field = fields["deployments"]
        assert dep_field.widget_type == "sequence"
        assert len(dep_field.children) == 1  # 1 deployment

    def test_deployment_item_contains_nested_sequence(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        dep_field = fields["deployments"]
        first_dep = dep_field.children[0]
        # Find the nested sequence among children
        nested_seqs = [c for c in first_dep.children if c.widget_type == "sequence"]
        assert len(nested_seqs) == 1
        nested_seq = nested_seqs[0]
        assert nested_seq.name == "deployments[0]/components"
        assert len(nested_seq.children) == 1  # 1 deployment component

    def test_nested_sequence_item_has_children(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        dep_field = fields["deployments"]
        first_dep = dep_field.children[0]
        nested_seqs = [c for c in first_dep.children if c.widget_type == "sequence"]
        nested_seq = nested_seqs[0]
        first_comp = nested_seq.children[0]
        assert first_comp.widget_type == "sequence_item"
        assert len(first_comp.children) == 3  # reference, image, pullPolicy


class TestDisplayCardRendering:
    def test_encrypted_fields_show_status(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        api_key_field = fields["config/api-key"]
        assert api_key_field.widget_type == "display_card"
        assert api_key_field.readonly is True
        assert "Versleuteld" in str(api_key_field.value)

    def test_truncated_field_shows_ellipsis(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        age_pub_field = fields["config/age-public-key"]
        assert age_pub_field.widget_type == "display_card"
        assert "..." in str(age_pub_field.value)


class TestConditionalVisibility:
    def test_hidden_editable_not_in_fields(self):
        """An editable with depends_on that isn't satisfied should be skipped."""
        renderer = _create_renderer()
        editable = ProjectEditable(
            yaml_path="conditional-field",
            widget="text",
            label="Conditional",
            depends_on="services",
            show_when={"contains": "nonexistent-service"},
        )
        fields = renderer._build_fields_from_editables(
            editables=[editable],
            yaml_data=SAMPLE_YAML,
        )
        assert "conditional-field" not in fields

    def test_visible_editable_in_fields(self):
        """An editable whose dependency is satisfied should be included."""
        renderer = _create_renderer()
        editable = ProjectEditable(
            yaml_path="conditional-field",
            widget="text",
            label="Conditional",
            depends_on="services",
            show_when={"contains": "keycloak"},
        )
        fields = renderer._build_fields_from_editables(
            editables=[editable],
            yaml_data=SAMPLE_YAML,
        )
        assert "conditional-field" in fields
