from __future__ import annotations

from opi.forms.editables.bridge import (
    editable_to_form_field,
    resolve_options_for_editable,
    should_render_editable,
)
from opi.forms.editables.editable import ProjectEditable


class TestEditableToFormField:
    def test_simple_text_editable(self):
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        yaml_data = {"name": "test-project"}
        field = editable_to_form_field(editable, yaml_data)
        assert field.name == "name"
        assert field.path == "name"
        assert field.widget_type == "text"
        assert field.label == "Naam"
        assert field.value == "test-project"
        assert field.schema_type is str
        assert field.converter is None  # MUST be None

    def test_with_converter_view(self):
        """converter.view() is applied to the YAML value."""
        from opi.forms.editables.converters import EncryptedDisplayConverter

        editable = ProjectEditable(
            yaml_path="config/api-key",
            widget="display-card",
            label="API Key",
            converter=EncryptedDisplayConverter(),
        )
        yaml_data = {"config": {"api-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata"}}
        field = editable_to_form_field(editable, yaml_data)
        assert field.value == "Versleuteld opgeslagen"

    def test_missing_value_returns_none(self):
        editable = ProjectEditable(yaml_path="missing", widget="text", label="Missing")
        field = editable_to_form_field(editable, {})
        assert field.value is None

    def test_with_errors(self):
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        errors = {"name": ["Dit veld is verplicht"]}
        field = editable_to_form_field(editable, {}, errors=errors)
        assert field.errors == ["Dit veld is verplicht"]

    def test_with_index_resolves_path(self):
        editable = ProjectEditable(yaml_path="users[*]/email", widget="text", label="Email")
        yaml_data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        field = editable_to_form_field(editable, yaml_data, index=1)
        assert field.path == "users[1]/email"
        assert field.value == "d@e.f"

    def test_readonly_on_edit(self):
        editable = ProjectEditable(
            yaml_path="name",
            widget="text",
            label="Naam",
            readonly_on_edit=True,
        )
        field_create = editable_to_form_field(editable, {"name": "x"}, edit_mode=False)
        field_edit = editable_to_form_field(editable, {"name": "x"}, edit_mode=True)
        assert field_create.readonly is False
        assert field_edit.readonly is True

    def test_readonly_always(self):
        editable = ProjectEditable(
            yaml_path="ns",
            widget="text",
            label="NS",
            readonly=True,
        )
        field = editable_to_form_field(editable, {"ns": "x"}, edit_mode=False)
        assert field.readonly is True

    def test_htmx_attrs_mapped(self):
        editable = ProjectEditable(
            yaml_path="services",
            widget="service-cards",
            label="Services",
            htmx_trigger="change",
            htmx_target="#config",
            htmx_swap="innerHTML",
        )
        field = editable_to_form_field(editable, {})
        assert field.htmx_attrs["hx-trigger"] == "change"
        assert field.htmx_attrs["hx-target"] == "#config"
        assert field.htmx_attrs["hx-swap"] == "innerHTML"

    def test_description_and_placeholder(self):
        editable = ProjectEditable(
            yaml_path="name",
            widget="text",
            label="Naam",
            description="Help text",
            placeholder="mijn-project",
        )
        field = editable_to_form_field(editable, {})
        assert field.description == "Help text"
        assert field.placeholder == "mijn-project"


class TestShouldRenderEditable:
    def test_no_dependency_always_true(self):
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        assert should_render_editable(editable, {}) is True

    def test_dependency_exists_truthy(self):
        editable = ProjectEditable(
            yaml_path="x",
            widget="text",
            label="X",
            depends_on="flag",
        )
        assert should_render_editable(editable, {"flag": True}) is True
        assert should_render_editable(editable, {"flag": "yes"}) is True

    def test_dependency_missing_false(self):
        editable = ProjectEditable(
            yaml_path="x",
            widget="text",
            label="X",
            depends_on="flag",
        )
        assert should_render_editable(editable, {}) is False

    def test_dependency_falsy_false(self):
        editable = ProjectEditable(
            yaml_path="x",
            widget="text",
            label="X",
            depends_on="flag",
        )
        assert should_render_editable(editable, {"flag": False}) is False
        assert should_render_editable(editable, {"flag": ""}) is False
        assert should_render_editable(editable, {"flag": []}) is False

    def test_show_when_contains_match(self):
        editable = ProjectEditable(
            yaml_path="x",
            widget="checkbox",
            label="X",
            depends_on="services",
            show_when={"contains": "keycloak"},
        )
        assert should_render_editable(editable, {"services": ["keycloak", "redis"]}) is True

    def test_show_when_contains_no_match(self):
        editable = ProjectEditable(
            yaml_path="x",
            widget="checkbox",
            label="X",
            depends_on="services",
            show_when={"contains": "keycloak"},
        )
        assert should_render_editable(editable, {"services": ["redis"]}) is False

    def test_show_when_contains_mixed_list(self):
        """Services can be mixed str/dict lists."""
        editable = ProjectEditable(
            yaml_path="x",
            widget="checkbox",
            label="X",
            depends_on="services",
            show_when={"contains": "keycloak"},
        )
        services = ["publish-on-web", {"keycloak": {"config": {}}}]
        assert should_render_editable(editable, {"services": services}) is True

    def test_show_when_value_list_match(self):
        editable = ProjectEditable(
            yaml_path="path",
            widget="text",
            label="Path",
            depends_on="components[0]/type",
            show_when={"type": ["single", "frontend"]},
        )
        assert should_render_editable(editable, {"components": [{"type": "single"}]}) is True

    def test_show_when_value_list_no_match(self):
        editable = ProjectEditable(
            yaml_path="path",
            widget="text",
            label="Path",
            depends_on="components[0]/type",
            show_when={"type": ["single", "frontend"]},
        )
        assert should_render_editable(editable, {"components": [{"type": "backend"}]}) is False


class TestResolveOptionsForEditable:
    def test_no_provider_returns_empty(self):
        editable = ProjectEditable(yaml_path="x", widget="text", label="X")
        assert resolve_options_for_editable(editable) == []

    def test_known_provider_returns_options(self):
        editable = ProjectEditable(
            yaml_path="cluster",
            widget="select",
            label="Cluster",
            options_provider="CpuLimitOptionsProvider",
        )
        options = resolve_options_for_editable(editable)
        assert len(options) > 0
        assert all("value" in o for o in options)

    def test_unknown_provider_returns_empty(self):
        editable = ProjectEditable(
            yaml_path="x",
            widget="select",
            label="X",
            options_provider="NonExistentProvider",
        )
        assert resolve_options_for_editable(editable) == []
