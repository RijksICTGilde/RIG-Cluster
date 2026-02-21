"""Tests for EditableFormProcessor."""

from __future__ import annotations

from opi.forms.editables.converters import IntegerListConverter
from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.validators import EmailValidator, SlugValidator


class FakeFormData:
    """Mimics FastAPI's ImmutableMultiDict for testing."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __iter__(self):
        return iter(self._data)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def getlist(self, key: str) -> list:
        val = self._data.get(key)
        if isinstance(val, list):
            return val
        return [val] if val is not None else []


SAMPLE_YAML = {
    "name": "test-project",
    "display-name": "Test Project",
    "description": "Een test project",
    "users": [
        {"email": "admin@test.nl", "role": "admin"},
        {"email": "dev@test.nl", "role": "developer"},
    ],
    "components": [
        {
            "name": "frontend",
            "type": "single",
            "ports": {"inbound": [8080], "outbound": [443]},
        },
    ],
    "services": ["publish-on-web"],
    "config": {
        "age-public-key": "age1abc...",
        "age-private-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata",
    },
}


class TestParseFormData:
    def test_simple_fields(self):
        processor = EditableFormProcessor()
        form_data = FakeFormData({"name": "test", "display-name": "Test"})
        editables = [
            ProjectEditable(yaml_path="name", widget="text", label="Naam"),
        ]
        parsed = processor.parse_form_data(form_data, editables)
        assert parsed["name"] == "test"
        assert parsed["display-name"] == "Test"

    def test_multi_value_fields(self):
        processor = EditableFormProcessor()
        form_data = FakeFormData({"clusters[]": ["local", "remote"]})
        editables = []
        parsed = processor.parse_form_data(form_data, editables)
        assert parsed["clusters"] == ["local", "remote"]

    def test_sequence_fields(self):
        processor = EditableFormProcessor()
        form_data = FakeFormData(
            {
                "users[0]/email": "admin@test.nl",
                "users[0]/role": "admin",
                "users[1]/email": "dev@test.nl",
                "users[1]/role": "developer",
            }
        )
        editables = []
        parsed = processor.parse_form_data(form_data, editables)
        assert parsed["users[0]/email"] == "admin@test.nl"
        assert parsed["users[1]/role"] == "developer"


class TestValidateEditables:
    def test_valid_data_returns_empty(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(yaml_path="name", widget="text", label="Naam", validator=SlugValidator()),
        ]
        parsed = {"name": "valid-name"}
        errors = processor.validate_editables(parsed, editables, {})
        assert errors == {}

    def test_invalid_slug_returns_error(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(yaml_path="name", widget="text", label="Naam", validator=SlugValidator()),
        ]
        parsed = {"name": "INVALID NAME!"}
        errors = processor.validate_editables(parsed, editables, {})
        assert "name" in errors
        assert len(errors["name"]) > 0

    def test_sequence_child_validation(self):
        processor = EditableFormProcessor()
        email_editable = ProjectEditable(
            yaml_path="users[*]/email", widget="text", label="Email", validator=EmailValidator()
        )
        seq_editable = ProjectEditable(yaml_path="users", widget="sequence", label="Users", children=[email_editable])
        parsed = {
            "users[0]/email": "valid@test.nl",
            "users[1]/email": "not-an-email",
        }
        yaml_data = {"users": [{"email": "a"}, {"email": "b"}]}
        errors = processor.validate_editables(parsed, [seq_editable], yaml_data)
        assert "users[0]/email" not in errors
        assert "users[1]/email" in errors

    def test_no_validator_no_error(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(yaml_path="description", widget="textarea", label="Desc"),
        ]
        parsed = {"description": "anything"}
        errors = processor.validate_editables(parsed, editables, {})
        assert errors == {}


class TestApplyToYaml:
    def test_writes_simple_value(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(yaml_path="display-name", widget="text", label="Naam"),
        ]
        parsed = {"display-name": "Updated Name"}
        result = processor.apply_to_yaml(parsed, editables, SAMPLE_YAML)
        assert result["display-name"] == "Updated Name"
        # Original is unchanged
        assert SAMPLE_YAML["display-name"] == "Test Project"

    def test_skips_readonly_fields(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(yaml_path="config/age-public-key", widget="display_card", label="Key", readonly=True),
        ]
        parsed = {"config/age-public-key": "hacked-value"}
        result = processor.apply_to_yaml(parsed, editables, SAMPLE_YAML)
        assert result["config"]["age-public-key"] == "age1abc..."

    def test_skips_readonly_on_edit_in_edit_mode(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(yaml_path="name", widget="text", label="Naam", readonly_on_edit=True),
        ]
        parsed = {"name": "new-name"}
        result = processor.apply_to_yaml(parsed, editables, SAMPLE_YAML, edit_mode=True)
        assert result["name"] == "test-project"

    def test_allows_readonly_on_edit_in_create_mode(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(yaml_path="name", widget="text", label="Naam", readonly_on_edit=True),
        ]
        parsed = {"name": "new-name"}
        result = processor.apply_to_yaml(parsed, editables, SAMPLE_YAML, edit_mode=False)
        assert result["name"] == "new-name"

    def test_applies_converter_write(self):
        processor = EditableFormProcessor()
        editables = [
            ProjectEditable(
                yaml_path="components[*]/ports/inbound",
                widget="text",
                label="Ports",
                converter=IntegerListConverter(),
            ),
        ]
        # This is a child editable, wrap it in a sequence for proper processing
        seq_editable = ProjectEditable(
            yaml_path="components",
            widget="sequence",
            label="Components",
            children=editables,
        )
        parsed = {"components[0]/ports/inbound": "8080, 9090"}
        result = processor.apply_to_yaml(parsed, [seq_editable], SAMPLE_YAML)
        assert result["components"][0]["ports"]["inbound"] == [8080, 9090]

    def test_preserves_encrypted_fields(self):
        processor = EditableFormProcessor()
        # Readonly fields should not be touched
        editables = [
            ProjectEditable(
                yaml_path="config/age-private-key",
                widget="display_card",
                label="Key",
                readonly=True,
            ),
        ]
        original_key = SAMPLE_YAML["config"]["age-private-key"]
        result = processor.apply_to_yaml({}, editables, SAMPLE_YAML)
        assert result["config"]["age-private-key"] == original_key

    def test_sequence_apply(self):
        processor = EditableFormProcessor()
        email_ed = ProjectEditable(yaml_path="users[*]/email", widget="text", label="Email")
        role_ed = ProjectEditable(yaml_path="users[*]/role", widget="select", label="Role")
        seq_ed = ProjectEditable(yaml_path="users", widget="sequence", label="Users", children=[email_ed, role_ed])
        parsed = {
            "users[0]/email": "new-admin@test.nl",
            "users[0]/role": "admin",
            "users[1]/email": "new-dev@test.nl",
            "users[1]/role": "developer",
        }
        result = processor.apply_to_yaml(parsed, [seq_ed], SAMPLE_YAML)
        assert result["users"][0]["email"] == "new-admin@test.nl"
        assert result["users"][1]["email"] == "new-dev@test.nl"


class TestNestedSequenceValidation:
    def test_nested_sequence_validation(self):
        processor = EditableFormProcessor()
        comp_ref = ProjectEditable(
            yaml_path="deployments[*]/components[*]/reference",
            widget="select",
            label="Ref",
            validator=SlugValidator(),
        )
        comp_seq = ProjectEditable(
            yaml_path="deployments[*]/components",
            widget="sequence",
            label="Comps",
            children=[comp_ref],
        )
        dep_seq = ProjectEditable(
            yaml_path="deployments",
            widget="sequence",
            label="Deps",
            children=[comp_seq],
        )
        yaml_data = {
            "deployments": [
                {"components": [{"reference": "valid"}, {"reference": "INVALID!"}]},
            ],
        }
        parsed = {
            "deployments[0]/components[0]/reference": "valid",
            "deployments[0]/components[1]/reference": "INVALID!",
        }
        errors = processor.validate_editables(parsed, [dep_seq], yaml_data)
        assert "deployments[0]/components[0]/reference" not in errors
        assert "deployments[0]/components[1]/reference" in errors
