"""
Form data processor for the editables pipeline.

Handles the reverse flow: form submission -> validation -> YAML update.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from opi.forms.editables.path import get_value, resolve_path
from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.forms.visualizers.bridge import should_render_editable

logger = logging.getLogger(__name__)


def _coerce_to_list(value: Any) -> list[Any]:
    """Ensure a value is a list.

    HTMX sends a single string when only one checkbox is checked in a
    checkbox_group. This helper normalises that to a list so downstream
    code (e.g. ``parse_services_from_strings``) never iterates over
    individual characters.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.visualizers.visualizer import EditableVisualizer


class EditableFormProcessor:
    """Processes form submissions through the editables pipeline."""

    def parse_form_data(
        self,
        form_data: Any,
        editables: list[EditableVisualizer],
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
        editables: list[EditableVisualizer],
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

        for vis in editables:
            ed = vis.editable
            if str(vis.widget) == "sequence":
                items = smart_get_value(yaml_data, ed.yaml_path) or []
                if not isinstance(items, list):
                    logger.debug("validate: %s not a list in yaml_data, skipping", ed.yaml_path)
                    continue
                logger.debug("validate: %s has %d items", ed.yaml_path, len(items))
                for index in range(len(items)):
                    for child_vis in vis.children or []:
                        child_ed = child_vis.editable
                        if str(child_vis.widget) == "sequence":
                            self._validate_nested_sequence(child_vis, parsed, yaml_data, errors, parent_index=index)
                        else:
                            concrete_path = resolve_path(child_ed.yaml_path, index)
                            value = parsed.get(concrete_path)
                            self._validate_field(child_vis, concrete_path, value, errors)
            else:
                value = parsed.get(ed.yaml_path)
                self._validate_field(vis, ed.yaml_path, value, errors)

        return errors

    @staticmethod
    def _validate_field(
        vis: EditableVisualizer,
        path: str,
        value: Any,
        errors: dict[str, list[str]],
    ) -> None:
        """Validate a single field for required and custom validator rules."""
        ed = vis.editable
        if ed.required and not value:
            errors.setdefault(path, []).append("Dit veld is verplicht")
            return
        if ed.validator:
            field_errors = ed.validator.validate(value)
            if field_errors:
                errors.setdefault(path, []).extend(field_errors)

    def _validate_nested_sequence(
        self,
        vis: EditableVisualizer,
        parsed: dict[str, Any],
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]],
        parent_index: int,
    ) -> None:
        """Validate children of a nested sequence."""
        ed = vis.editable
        concrete_path = resolve_path(ed.yaml_path, parent_index)
        items = smart_get_value(yaml_data, concrete_path) or []
        if not isinstance(items, list):
            logger.debug("validate_nested: %s not a list, skipping", concrete_path)
            return
        logger.debug("validate_nested: %s has %d items", concrete_path, len(items))
        for child_index in range(len(items)):
            for child_vis in vis.children or []:
                child_ed = child_vis.editable
                child_path = resolve_path(child_ed.yaml_path, parent_index)
                child_path = resolve_path(child_path, child_index)
                value = parsed.get(child_path)
                self._validate_field(child_vis, child_path, value, errors)

    def enforce_sections(
        self,
        yaml_data: dict[str, Any],
        sections: list[FormSection],
    ) -> list[str]:
        """
        Run section-level enforcers.

        Returns:
            List of global error messages. Empty means all passed.
        """
        global_errors: list[str] = []
        for section in sections:
            if section.enforcer:
                try:
                    section.enforcer.enforce(yaml_data, {})
                except ValueError as e:
                    global_errors.append(str(e))
        return global_errors

    def enforce_parts(
        self,
        yaml_data: dict[str, Any],
        sections: list[FormSection],
    ) -> list[str]:
        """Backward compatibility alias for enforce_sections."""
        return self.enforce_sections(yaml_data, sections)

    def clear_hidden_depends_on(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
    ) -> None:
        """Clear values for editables whose depends_on condition is not met.

        When a toggle is turned off, dependent fields should have their
        values removed from the YAML data so they don't persist.
        Mutates ``yaml_data`` in place.
        """
        for vis in editables:
            ed = vis.editable
            if not ed.depends_on:
                continue
            if not should_render_editable(vis, yaml_data):
                # Remove the value from YAML
                smart_set_value(yaml_data, ed.yaml_path, None)

    def apply_generators(
        self,
        editables: list[Editable],
        yaml_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Run generators on computed editables and merge results into YAML.

        Generators are executed in list order, so order matters when
        one generator depends on values produced by a previous one
        (e.g., encrypted private key depends on the public key).

        Returns:
            New yaml_data dict with generated values applied.
        """
        result = copy.deepcopy(yaml_data)

        for editable in editables:
            if editable.generator:
                value = editable.generator.generate(result)
                smart_set_value(result, editable.yaml_path, value)

        # Clean up temp data used by generators
        result.pop("_generated", None)
        return result

    def apply_to_yaml(
        self,
        parsed: dict[str, Any],
        editables: list[EditableVisualizer],
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

        for vis in editables:
            ed = vis.editable
            if vis.readonly:
                continue
            if vis.readonly_on_edit and edit_mode:
                continue

            widget = str(vis.widget)
            if widget == "sequence":
                self._apply_sequence_to_yaml(vis, parsed, result, edit_mode)
            elif widget == "checkbox":
                # Unchecked checkboxes are absent from form data; treat as False.
                raw = parsed.get(ed.yaml_path)
                value: Any = bool(raw) if raw else False
                if ed.converter:
                    value = ed.converter.write(value)
                smart_set_value(result, ed.yaml_path, value)
            elif widget == "checkbox_group":
                value = _coerce_to_list(parsed.get(ed.yaml_path))
                if ed.converter:
                    value = ed.converter.write(value)
                smart_set_value(result, ed.yaml_path, value)
            else:
                value = parsed.get(ed.yaml_path)
                if value is not None:
                    if ed.converter:
                        value = ed.converter.write(value)
                    smart_set_value(result, ed.yaml_path, value)

        return result

    def _apply_sequence_to_yaml(
        self,
        vis: EditableVisualizer,
        parsed: dict[str, Any],
        yaml_data: dict[str, Any],
        edit_mode: bool,
    ) -> None:
        """Apply sequence field values back to YAML."""
        ed = vis.editable
        items = smart_get_value(yaml_data, ed.yaml_path) or []
        if not isinstance(items, list):
            return
        for index in range(len(items)):
            for child_vis in vis.children or []:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                if str(child_vis.widget) == "sequence":
                    self._apply_nested_sequence_to_yaml(child_vis, parsed, yaml_data, edit_mode, parent_index=index)
                elif str(child_vis.widget) == "checkbox_group":
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = _coerce_to_list(parsed.get(concrete_path))
                    if child_ed.converter:
                        value = child_ed.converter.write(value)
                    smart_set_value(yaml_data, concrete_path, value)
                else:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = parsed.get(concrete_path)
                    if value is not None:
                        if child_ed.converter:
                            value = child_ed.converter.write(value)
                        smart_set_value(yaml_data, concrete_path, value)

    def _apply_nested_sequence_to_yaml(
        self,
        vis: EditableVisualizer,
        parsed: dict[str, Any],
        yaml_data: dict[str, Any],
        edit_mode: bool,
        parent_index: int,
    ) -> None:
        """Apply nested sequence values back to YAML."""
        ed = vis.editable
        concrete_path = resolve_path(ed.yaml_path, parent_index)
        items = smart_get_value(yaml_data, concrete_path) or []
        if not isinstance(items, list):
            return
        for child_index in range(len(items)):
            for child_vis in vis.children or []:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                child_path = resolve_path(child_ed.yaml_path, parent_index)
                child_path = resolve_path(child_path, child_index)
                value = parsed.get(child_path)
                if value is not None:
                    if child_ed.converter:
                        value = child_ed.converter.write(value)
                    smart_set_value(yaml_data, child_path, value)

    # ------------------------------------------------------------------
    # JSON submission pipeline (nested structure, no flat intermediate)
    # ------------------------------------------------------------------

    def process_json_submission(
        self,
        submitted: dict[str, Any],
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        edit_mode: bool = False,
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """Process a nested JSON form submission in a single pass.

        Reads values from the nested JSON using plain path traversal
        (``get_value``), validates them, applies ``converter.write()``,
        and writes to a deep-copy of *yaml_data* using ``smart_set_value``
        (which correctly handles the mixed services list format).

        The key improvement over the flat-key pipeline: **sequence item
        counts come from the submitted data** (the form's truth), not
        from stale session state.

        Returns:
            ``(result_yaml, errors)`` tuple.
        """
        result = copy.deepcopy(yaml_data)
        errors: dict[str, list[str]] = {}

        for vis in editables:
            ed = vis.editable
            if vis.readonly or (vis.readonly_on_edit and edit_mode):
                continue

            widget = str(vis.widget)
            if widget == "sequence":
                self._process_sequence_json(
                    vis,
                    submitted,
                    result,
                    errors,
                    edit_mode,
                )
            elif widget == "checkbox":
                # Unchecked checkboxes are absent from JSON; treat as False.
                raw = get_value(submitted, ed.yaml_path)
                value: Any = bool(raw) if raw else False
                self._validate_field(vis, ed.yaml_path, value, errors)
                if ed.converter:
                    value = ed.converter.write(value)
                smart_set_value(result, ed.yaml_path, value)
            elif widget == "checkbox_group":
                value = _coerce_to_list(get_value(submitted, ed.yaml_path))
                self._validate_field(vis, ed.yaml_path, value, errors)
                if ed.converter:
                    value = ed.converter.write(value)
                smart_set_value(result, ed.yaml_path, value)
            else:
                value = get_value(submitted, ed.yaml_path)
                self._validate_field(vis, ed.yaml_path, value, errors)
                if value is not None:
                    if ed.converter:
                        value = ed.converter.write(value)
                    smart_set_value(result, ed.yaml_path, value)

        return result, errors

    def _process_sequence_json(
        self,
        vis: EditableVisualizer,
        submitted: dict[str, Any],
        result: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
    ) -> None:
        """Process a sequence editable from nested JSON.

        Reads the items array from ``submitted`` (source of truth for
        item count), deep-copies it into ``result``, then validates
        and applies converters for each child editable.
        """
        ed = vis.editable
        items = get_value(submitted, ed.yaml_path)
        if not isinstance(items, list):
            items = []

        # Write the submitted items into result (correct count + raw values)
        smart_set_value(result, ed.yaml_path, copy.deepcopy(items))

        for index in range(len(items)):
            for child_vis in vis.children or []:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                if str(child_vis.widget) == "sequence":
                    self._process_nested_sequence_json(
                        child_vis,
                        submitted,
                        result,
                        errors,
                        edit_mode,
                        index,
                    )
                elif str(child_vis.widget) == "checkbox_group":
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = _coerce_to_list(get_value(submitted, concrete_path))
                    self._validate_field(child_vis, concrete_path, value, errors)
                    if child_ed.converter:
                        value = child_ed.converter.write(value)
                    smart_set_value(result, concrete_path, value)
                else:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = get_value(submitted, concrete_path)
                    self._validate_field(child_vis, concrete_path, value, errors)
                    if value is not None:
                        if child_ed.converter:
                            value = child_ed.converter.write(value)
                        smart_set_value(result, concrete_path, value)

    def _process_nested_sequence_json(
        self,
        vis: EditableVisualizer,
        submitted: dict[str, Any],
        result: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        parent_index: int,
    ) -> None:
        """Process a nested sequence from JSON (e.g. additional-clients[0]/redirect-uris)."""
        ed = vis.editable
        concrete_seq_path = resolve_path(ed.yaml_path, parent_index)
        items = get_value(submitted, concrete_seq_path)
        if not isinstance(items, list):
            items = []

        # Write the nested list (correct count)
        smart_set_value(result, concrete_seq_path, copy.deepcopy(items))

        for child_index in range(len(items)):
            for child_vis in vis.children or []:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                child_path = resolve_path(child_ed.yaml_path, parent_index)
                child_path = resolve_path(child_path, child_index)
                value = get_value(submitted, child_path)
                self._validate_field(child_vis, child_path, value, errors)
                if value is not None:
                    if child_ed.converter:
                        value = child_ed.converter.write(value)
                    smart_set_value(result, child_path, value)
