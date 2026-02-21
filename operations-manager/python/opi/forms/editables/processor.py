"""
Form data processor for the editables pipeline.

Handles the reverse flow: form submission -> validation -> YAML update.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from opi.forms.editables.path import get_value, resolve_path, set_value

if TYPE_CHECKING:
    from opi.forms.editables.editable import ProjectEditable
    from opi.forms.editables.part import EditablePart


class EditableFormProcessor:
    """Processes form submissions through the editables pipeline."""

    def parse_form_data(
        self,
        form_data: Any,
        editables: list[ProjectEditable],
    ) -> dict[str, Any]:
        """
        Parse flat HTML form data into a dict keyed by YAML paths.

        HTML form names use the YAML path format (e.g., "users[0]/email",
        "components[1]/ports/inbound"). Multi-value fields (checkboxes)
        use "path[]" naming convention.

        Returns:
            dict mapping yaml_path -> submitted value
        """
        parsed: dict[str, Any] = {}

        for key in form_data:
            if key.endswith("[]"):
                parsed[key.rstrip("[]")] = form_data.getlist(key)
            else:
                parsed[key] = form_data.get(key)

        return parsed

    def validate_editables(
        self,
        parsed: dict[str, Any],
        editables: list[ProjectEditable],
        yaml_data: dict[str, Any],
    ) -> dict[str, list[str]]:
        """
        Run each editable's validator on the parsed form data.

        For sequence editables, validates each item's child editables.

        Returns:
            dict mapping yaml_path -> list of error messages.
            Empty dict means no errors.
        """
        errors: dict[str, list[str]] = {}

        for editable in editables:
            if editable.widget == "sequence":
                items = get_value(yaml_data, editable.yaml_path) or []
                if not isinstance(items, list):
                    continue
                for index in range(len(items)):
                    for child in editable.children or []:
                        if child.widget == "sequence":
                            # Nested sequence validation
                            self._validate_nested_sequence(child, parsed, yaml_data, errors, parent_index=index)
                        elif child.validator:
                            concrete_path = resolve_path(child.yaml_path, index)
                            value = parsed.get(concrete_path)
                            field_errors = child.validator.validate(value)
                            if field_errors:
                                errors[concrete_path] = field_errors
            elif editable.validator:
                value = parsed.get(editable.yaml_path)
                field_errors = editable.validator.validate(value)
                if field_errors:
                    errors[editable.yaml_path] = field_errors

        return errors

    def _validate_nested_sequence(
        self,
        editable: ProjectEditable,
        parsed: dict[str, Any],
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]],
        parent_index: int,
    ) -> None:
        """Validate children of a nested sequence."""
        concrete_path = resolve_path(editable.yaml_path, parent_index)
        items = get_value(yaml_data, concrete_path) or []
        if not isinstance(items, list):
            return
        for child_index in range(len(items)):
            for child in editable.children or []:
                if child.validator:
                    # Resolve both parent and child wildcards
                    child_path = resolve_path(child.yaml_path, parent_index)
                    child_path = resolve_path(child_path, child_index)
                    value = parsed.get(child_path)
                    field_errors = child.validator.validate(value)
                    if field_errors:
                        errors[child_path] = field_errors

    def enforce_parts(
        self,
        yaml_data: dict[str, Any],
        parts: list[EditablePart],
    ) -> list[str]:
        """
        Run part-level enforcers.

        Returns:
            List of global error messages. Empty means all passed.
        """
        global_errors: list[str] = []
        for part in parts:
            if part.enforcer:
                try:
                    part.enforcer.enforce(yaml_data, {})
                except ValueError as e:
                    global_errors.append(str(e))
        return global_errors

    def apply_to_yaml(
        self,
        parsed: dict[str, Any],
        editables: list[ProjectEditable],
        yaml_data: dict[str, Any],
        edit_mode: bool = False,
    ) -> dict[str, Any]:
        """
        Write validated form values back into the YAML dict.

        - Deep-copies yaml_data first (preserves original)
        - Skips readonly fields
        - Skips readonly_on_edit fields when edit_mode=True
        - Applies converter.write() before set_value()

        Returns:
            New yaml_data dict with values applied.
        """
        result = copy.deepcopy(yaml_data)

        for editable in editables:
            if editable.readonly:
                continue
            if editable.readonly_on_edit and edit_mode:
                continue

            if editable.widget == "sequence":
                self._apply_sequence_to_yaml(editable, parsed, result, edit_mode)
            else:
                value = parsed.get(editable.yaml_path)
                if value is not None:
                    if editable.converter:
                        value = editable.converter.write(value)
                    set_value(result, editable.yaml_path, value)

        return result

    def _apply_sequence_to_yaml(
        self,
        editable: ProjectEditable,
        parsed: dict[str, Any],
        yaml_data: dict[str, Any],
        edit_mode: bool,
    ) -> None:
        """Apply sequence field values back to YAML."""
        items = get_value(yaml_data, editable.yaml_path) or []
        if not isinstance(items, list):
            return
        for index in range(len(items)):
            for child in editable.children or []:
                if child.readonly or (child.readonly_on_edit and edit_mode):
                    continue
                if child.widget == "sequence":
                    self._apply_nested_sequence_to_yaml(child, parsed, yaml_data, edit_mode, parent_index=index)
                else:
                    concrete_path = resolve_path(child.yaml_path, index)
                    value = parsed.get(concrete_path)
                    if value is not None:
                        if child.converter:
                            value = child.converter.write(value)
                        set_value(yaml_data, concrete_path, value)

    def _apply_nested_sequence_to_yaml(
        self,
        editable: ProjectEditable,
        parsed: dict[str, Any],
        yaml_data: dict[str, Any],
        edit_mode: bool,
        parent_index: int,
    ) -> None:
        """Apply nested sequence values back to YAML."""
        concrete_path = resolve_path(editable.yaml_path, parent_index)
        items = get_value(yaml_data, concrete_path) or []
        if not isinstance(items, list):
            return
        for child_index in range(len(items)):
            for child in editable.children or []:
                if child.readonly or (child.readonly_on_edit and edit_mode):
                    continue
                child_path = resolve_path(child.yaml_path, parent_index)
                child_path = resolve_path(child_path, child_index)
                value = parsed.get(child_path)
                if value is not None:
                    if child.converter:
                        value = child.converter.write(value)
                    set_value(yaml_data, child_path, value)
