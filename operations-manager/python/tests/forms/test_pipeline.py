"""Tests for the data-only editable validation pipeline."""

from __future__ import annotations

from opi.forms.editables.converters import ContainerImageConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.enforcers import ComponentServicesEnforcer
from opi.forms.editables.pipeline import (
    convert_fields,
    enforce_rules,
    validate_field,
    validate_fields,
)
from opi.forms.editables.validators import (
    AllowedValuesValidator,
    ComponentNameValidator,
    ContainerImageValidator,
    PathValidator,
)

# ---------------------------------------------------------------------------
# validate_field
# ---------------------------------------------------------------------------


class TestValidateField:
    def test_valid_value(self):
        editable = Editable(yaml_path="x", validator=ComponentNameValidator())
        assert validate_field(editable, "myapp") == []

    def test_invalid_value(self):
        editable = Editable(yaml_path="x", validator=ComponentNameValidator())
        errors = validate_field(editable, "MY-APP")
        assert len(errors) == 1

    def test_required_missing(self):
        editable = Editable(yaml_path="x", required=True)
        errors = validate_field(editable, None)
        assert len(errors) == 1

    def test_required_empty_string(self):
        editable = Editable(yaml_path="x", required=True)
        errors = validate_field(editable, "  ")
        assert len(errors) == 1

    def test_required_present(self):
        editable = Editable(yaml_path="x", required=True)
        assert validate_field(editable, "hello") == []

    def test_none_value_skips_validator(self):
        editable = Editable(yaml_path="x", validator=ComponentNameValidator())
        assert validate_field(editable, None) == []

    def test_empty_string_skips_validator(self):
        editable = Editable(yaml_path="x", validator=ComponentNameValidator())
        assert validate_field(editable, "") == []

    def test_no_validator(self):
        editable = Editable(yaml_path="x")
        assert validate_field(editable, "anything") == []

    def test_container_image_uppercase(self):
        editable = Editable(yaml_path="x", validator=ContainerImageValidator())
        errors = validate_field(editable, "Nginx:Latest")
        assert len(errors) == 1
        assert "kleine letters" in errors[0]

    def test_container_image_valid(self):
        editable = Editable(yaml_path="x", validator=ContainerImageValidator())
        assert validate_field(editable, "nginx:latest") == []

    def test_path_no_leading_slash(self):
        editable = Editable(yaml_path="x", validator=PathValidator())
        errors = validate_field(editable, "api/v1")
        assert len(errors) == 1

    def test_path_valid(self):
        editable = Editable(yaml_path="x", validator=PathValidator())
        assert validate_field(editable, "/api/v1") == []

    def test_allowed_values_invalid(self):
        editable = Editable(yaml_path="x", validator=AllowedValuesValidator(["500m", "1"]))
        errors = validate_field(editable, "2")
        assert len(errors) == 1

    def test_allowed_values_valid(self):
        editable = Editable(yaml_path="x", validator=AllowedValuesValidator(["500m", "1"]))
        assert validate_field(editable, "500m") == []

    def test_component_name_too_long(self):
        editable = Editable(yaml_path="x", validator=ComponentNameValidator())
        errors = validate_field(editable, "a" + "b" * 40)  # 41 chars
        assert len(errors) == 1


# ---------------------------------------------------------------------------
# validate_fields
# ---------------------------------------------------------------------------


class TestValidateFields:
    def test_all_valid(self):
        editables = {
            "name": Editable(yaml_path="x", validator=ComponentNameValidator(), required=True),
            "image": Editable(yaml_path="y", validator=ContainerImageValidator()),
        }
        errors = validate_fields({"name": "myapp", "image": "nginx:latest"}, editables)
        assert errors == {}

    def test_multiple_errors(self):
        editables = {
            "name": Editable(yaml_path="x", validator=ComponentNameValidator(), required=True),
            "image": Editable(yaml_path="y", validator=ContainerImageValidator()),
        }
        errors = validate_fields({"name": "MY-APP", "image": "Nginx:Latest"}, editables)
        assert "name" in errors
        assert "image" in errors

    def test_missing_required_field(self):
        editables = {
            "name": Editable(yaml_path="x", required=True),
        }
        errors = validate_fields({}, editables)
        assert "name" in errors

    def test_extra_fields_ignored(self):
        editables = {
            "name": Editable(yaml_path="x", validator=ComponentNameValidator()),
        }
        errors = validate_fields({"name": "myapp", "extra": "ignored"}, editables)
        assert errors == {}

    def test_optional_absent_field_ok(self):
        editables = {
            "cpu_limit": Editable(yaml_path="x", validator=AllowedValuesValidator(["500m", "1"])),
        }
        errors = validate_fields({}, editables)
        assert errors == {}


# ---------------------------------------------------------------------------
# convert_fields
# ---------------------------------------------------------------------------


class TestConvertFields:
    def test_converter_applied(self):
        editables = {
            "image": Editable(yaml_path="x", converter=ContainerImageConverter()),
        }
        result = convert_fields({"image": "Nginx:Latest"}, editables)
        assert result["image"] == "nginx:latest"

    def test_no_converter_passthrough(self):
        editables = {
            "name": Editable(yaml_path="x"),
        }
        result = convert_fields({"name": "myapp"}, editables)
        assert result["name"] == "myapp"

    def test_none_value_not_converted(self):
        editables = {
            "image": Editable(yaml_path="x", converter=ContainerImageConverter()),
        }
        result = convert_fields({"image": None}, editables)
        assert result["image"] is None

    def test_unregistered_field_passthrough(self):
        editables = {}
        result = convert_fields({"extra": "value"}, editables)
        assert result["extra"] == "value"


# ---------------------------------------------------------------------------
# enforce_rules
# ---------------------------------------------------------------------------


class TestEnforceRules:
    async def test_no_enforcers(self):
        assert await enforce_rules({"key": "value"}, []) == []

    async def test_enforcer_passes(self):
        data = {
            "services": ["publish-on-web"],
            "components": [{"name": "app", "services": ["publish-on-web"]}],
        }
        errors = await enforce_rules(data, [ComponentServicesEnforcer()])
        assert errors == []

    async def test_enforcer_fails(self):
        data = {
            "services": ["publish-on-web"],
            "components": [{"name": "app", "services": ["postgresql-database"]}],
        }
        errors = await enforce_rules(data, [ComponentServicesEnforcer()])
        assert len(errors) == 1
        assert "postgresql-database" in errors[0]
