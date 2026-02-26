"""Tests for the core editables dataclasses and protocols."""

from opi.forms.editables.editable import (
    EditableConverter,
    EditableEnforcer,
    EditableValidator,
    ProjectEditable,
)
from opi.forms.editables.flow import FlowMode, FormFlow
from opi.forms.editables.section import FormSection
from opi.forms.layout import LayoutElement


class TestProjectEditable:
    def test_minimal_instantiation(self):
        """Only required fields: yaml_path, widget, label."""
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        assert editable.yaml_path == "name"
        assert editable.widget == "text"
        assert editable.label == "Naam"
        assert editable.readonly is False
        assert editable.required is False
        assert editable.children is None
        assert editable.description is None
        assert editable.placeholder is None
        assert editable.options_provider is None
        assert editable.converter is None
        assert editable.validator is None
        assert editable.enforcer is None
        assert editable.readonly_on_edit is False
        assert editable.depends_on is None
        assert editable.show_when is None
        assert editable.htmx_trigger is None
        assert editable.htmx_target is None
        assert editable.htmx_swap is None
        assert editable.min_items == 0
        assert editable.max_items is None
        assert editable.attributes is None

    def test_all_fields_populated(self):
        """All optional fields set."""

        class StubConverter:
            def read(self, value):
                return value

            def write(self, value):
                return value

            def view(self, value):
                return str(value)

        class StubValidator:
            def validate(self, value):
                return []

        class StubEnforcer:
            def enforce(self, value, context):
                return value

        editable = ProjectEditable(
            yaml_path="components[*]/name",
            widget="text",
            label="component.name",
            description="component.name.description",
            placeholder="component-1",
            options_provider="ComponentTypeOptionsProvider",
            converter=StubConverter(),
            validator=StubValidator(),
            enforcer=StubEnforcer(),
            readonly=True,
            readonly_on_edit=True,
            required=True,
            children=[],
            depends_on="components[*]/type",
            show_when={"type": ["single", "frontend"]},
            htmx_trigger="change",
            htmx_target="#target",
            htmx_swap="innerHTML",
            min_items=1,
            max_items=10,
        )
        assert editable.yaml_path == "components[*]/name"
        assert editable.description == "component.name.description"
        assert editable.placeholder == "component-1"
        assert editable.options_provider == "ComponentTypeOptionsProvider"
        assert editable.converter is not None
        assert editable.validator is not None
        assert editable.enforcer is not None
        assert editable.readonly is True
        assert editable.readonly_on_edit is True
        assert editable.required is True
        assert editable.children == []
        assert editable.depends_on == "components[*]/type"
        assert editable.show_when == {"type": ["single", "frontend"]}
        assert editable.htmx_trigger == "change"
        assert editable.htmx_target == "#target"
        assert editable.htmx_swap == "innerHTML"
        assert editable.min_items == 1
        assert editable.max_items == 10

    def test_children_list(self):
        """Children can hold nested ProjectEditables."""
        child = ProjectEditable(yaml_path="users[*]/email", widget="text", label="Email")
        parent = ProjectEditable(
            yaml_path="users",
            widget="sequence",
            label="Gebruikers",
            children=[child],
        )
        assert parent.children is not None
        assert len(parent.children) == 1
        assert parent.children[0].yaml_path == "users[*]/email"

    def test_nested_children(self):
        """Children can be nested multiple levels deep."""
        grandchild = ProjectEditable(yaml_path="components[*]/storage[*]/name", widget="text", label="Naam")
        child = ProjectEditable(
            yaml_path="components[*]/storage",
            widget="sequence",
            label="Opslag",
            children=[grandchild],
        )
        parent = ProjectEditable(
            yaml_path="components",
            widget="sequence",
            label="Componenten",
            children=[child],
        )
        assert parent.children is not None
        assert parent.children[0].children is not None
        assert parent.children[0].children[0].yaml_path == "components[*]/storage[*]/name"


class TestFormSection:
    def test_minimal_instantiation(self):
        """Only required fields: section_id, title."""
        section = FormSection(section_id="identity", title="Project")
        assert section.section_id == "identity"
        assert section.title == "Project"
        assert section.editables == []
        assert section.visible is True
        assert section.is_readonly is False
        assert section.icon is None
        assert section.description is None
        assert section.layout is None
        assert section.summary_fn is None
        assert section.enforcer is None

    def test_with_editables(self):
        """Section with populated editables list."""
        name = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        desc = ProjectEditable(yaml_path="description", widget="textarea", label="Beschrijving")
        section = FormSection(
            section_id="identity",
            title="Project",
            editables=[name, desc],
        )
        assert len(section.editables) == 2
        assert section.editables[0].yaml_path == "name"
        assert section.editables[1].yaml_path == "description"

    def test_with_layout(self):
        """Section with a layout element."""
        layout = LayoutElement(css_class="test-class")
        section = FormSection(
            section_id="identity",
            title="Project",
            layout=layout,
        )
        assert section.layout is not None
        assert section.layout.css_class == "test-class"

    def test_with_summary_fn(self):
        """Section with a summary function."""

        def my_summary(data: dict) -> str:
            return f"Project: {data.get('name', 'onbekend')}"

        section = FormSection(
            section_id="identity",
            title="Project",
            summary_fn=my_summary,
        )
        assert section.summary_fn is not None
        assert section.summary_fn({"name": "test"}) == "Project: test"

    def test_readonly_section(self):
        """Read-only section (e.g., config display)."""
        section = FormSection(
            section_id="config",
            title="Configuratie",
            is_readonly=True,
            visible=False,
        )
        assert section.is_readonly is True
        assert section.visible is False

    def test_conditional_visibility(self):
        """Section with callable visibility."""
        section = FormSection(
            section_id="keycloak-config",
            title="Keycloak",
            visible=lambda data: "keycloak" in data.get("services", []),
        )
        assert callable(section.visible)
        assert section.visible({"services": ["keycloak"]}) is True
        assert section.visible({"services": []}) is False


class TestFormFlow:
    def test_wizard_mode(self):
        flow = FormFlow(flow_id="create", title="Aanmaken", mode=FlowMode.WIZARD)
        assert flow.mode == FlowMode.WIZARD
        assert flow.show_review is True
        assert flow.sections == []
        assert flow.htmx_base_url == ""
        assert flow.save_per_section is True

    def test_tabs_mode(self):
        flow = FormFlow(
            flow_id="edit",
            title="Bewerken",
            mode=FlowMode.TABS,
            htmx_base_url="/projects/test/sections",
        )
        assert flow.mode == FlowMode.TABS
        assert flow.htmx_base_url == "/projects/test/sections"

    def test_with_sections(self):
        """Flow with multiple sections in order."""
        s1 = FormSection(section_id="identity", title="Project")
        s2 = FormSection(section_id="services", title="Services")
        s3 = FormSection(section_id="users", title="Team")
        flow = FormFlow(
            flow_id="create",
            title="Aanmaken",
            mode=FlowMode.WIZARD,
            sections=[s1, s2, s3],
        )
        assert len(flow.sections) == 3
        assert flow.sections[0].section_id == "identity"
        assert flow.sections[1].section_id == "services"
        assert flow.sections[2].section_id == "users"

    def test_flow_mode_values(self):
        """FlowMode enum values match expected strings."""
        assert FlowMode.WIZARD.value == "wizard"
        assert FlowMode.TABS.value == "tabs"


class TestProtocols:
    def test_converter_is_runtime_checkable(self):
        """A class implementing read/write/view satisfies EditableConverter."""

        class MyConverter:
            def read(self, value):
                return value

            def write(self, value):
                return value

            def view(self, value):
                return str(value)

        assert isinstance(MyConverter(), EditableConverter)

    def test_validator_is_runtime_checkable(self):
        """A class implementing validate satisfies EditableValidator."""

        class MyValidator:
            def validate(self, value):
                return []

        assert isinstance(MyValidator(), EditableValidator)

    def test_enforcer_is_runtime_checkable(self):
        """A class implementing enforce satisfies EditableEnforcer."""

        class MyEnforcer:
            def enforce(self, value, context):
                return value

        assert isinstance(MyEnforcer(), EditableEnforcer)

    def test_non_converter_fails_check(self):
        """A class missing methods does not satisfy EditableConverter."""

        class NotAConverter:
            def read(self, value):
                return value

        assert not isinstance(NotAConverter(), EditableConverter)

    def test_non_validator_fails_check(self):
        """A class missing validate does not satisfy EditableValidator."""

        class NotAValidator:
            pass

        assert not isinstance(NotAValidator(), EditableValidator)

    def test_non_enforcer_fails_check(self):
        """A class missing enforce does not satisfy EditableEnforcer."""

        class NotAnEnforcer:
            pass

        assert not isinstance(NotAnEnforcer(), EditableEnforcer)

    def test_converter_methods_callable(self):
        """Converter protocol methods work when called."""

        class UpperConverter:
            def read(self, value):
                return str(value).upper() if value else ""

            def write(self, value):
                return str(value).lower() if value else ""

            def view(self, value):
                return f"[{value}]"

        converter = UpperConverter()
        assert converter.read("hello") == "HELLO"
        assert converter.write("HELLO") == "hello"
        assert converter.view("hello") == "[hello]"

    def test_validator_returns_errors(self):
        """Validator can return error messages."""

        class LengthValidator:
            def validate(self, value):
                if not value:
                    return ["Waarde is vereist"]
                if len(str(value)) < 3:
                    return ["Minimaal 3 tekens vereist"]
                return []

        validator = LengthValidator()
        assert validator.validate("") == ["Waarde is vereist"]
        assert validator.validate("ab") == ["Minimaal 3 tekens vereist"]
        assert validator.validate("abc") == []

    def test_enforcer_raises_on_violation(self):
        """Enforcer can raise ValueError."""
        import pytest

        class RequiredEnforcer:
            def enforce(self, value, context):
                if not value:
                    msg = "Waarde is vereist"
                    raise ValueError(msg)
                return value

        enforcer = RequiredEnforcer()
        assert enforcer.enforce("ok", {}) == "ok"
        with pytest.raises(ValueError, match="Waarde is vereist"):
            enforcer.enforce("", {})
