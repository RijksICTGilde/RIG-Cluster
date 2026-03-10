"""
Form data processor for the editables pipeline.

Handles the reverse flow: form submission -> validation -> YAML update.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.path import get_value, resolve_path
from opi.forms.editables.service_path import smart_delete_value, smart_get_value, smart_set_value
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


def _converter_write(converter: Any, value: Any, yaml_data: dict[str, Any] | None = None) -> Any:
    """Call converter.write() passing yaml_data when accepted."""
    try:
        return converter.write(value, yaml_data=yaml_data)
    except TypeError:
        return converter.write(value)


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

    async def validate_editables(
        self,
        parsed: dict[str, Any],
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        enforcer_context: dict[str, Any] | None = None,
    ) -> dict[str, list[str]]:
        """
        Run each editable's validator on the parsed form data.

        For sequence editables, validates each item's child editables.

        Args:
            enforcer_context: Optional metadata for enforcers (e.g. project_name,
                edit_mode). Separate from yaml_data to avoid polluting form data.

        Returns:
            dict mapping yaml_path -> list of error messages.
            Empty dict means no errors.
        """
        errors: dict[str, list[str]] = {}
        ctx = enforcer_context or {}

        for vis in editables:
            ed = vis.editable
            # Skip validation for fields hidden by depends_on/show_when conditions
            if not should_render_editable(vis, yaml_data, siblings=editables):
                continue
            if vis.widget == WidgetType.GROUP:
                # Recurse into group children, then run parent enforcer
                group_errors = await self.validate_editables(parsed, vis.children or [], yaml_data, ctx)
                errors.update(group_errors)
                if not group_errors and ed.enforcer:
                    try:
                        await ed.enforcer.enforce(yaml_data, ctx)
                    except ValueError as e:
                        errors.setdefault(ed.yaml_path, []).append(str(e))
            elif vis.widget == WidgetType.SEQUENCE:
                items = smart_get_value(yaml_data, ed.yaml_path) or []
                if not isinstance(items, list):
                    logger.debug("validate: %s not a list in yaml_data, skipping", ed.yaml_path)
                    continue
                logger.debug("validate: %s has %d items", ed.yaml_path, len(items))
                for index in range(len(items)):
                    for child_vis in vis.children or []:
                        child_ed = child_vis.editable
                        if child_vis.widget == WidgetType.SEQUENCE:
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

    async def enforce_sections(
        self,
        yaml_data: dict[str, Any],
        sections: list[FormSection],
        enforcer_context: dict[str, Any] | None = None,
    ) -> list[str]:
        """
        Run section-level enforcers.

        Args:
            enforcer_context: Optional metadata for enforcers (e.g. project_name).

        Returns:
            List of global error messages. Empty means all passed.
        """
        ctx = enforcer_context or {}
        global_errors: list[str] = []
        for section in sections:
            if section.enforcer:
                try:
                    await section.enforcer.enforce(yaml_data, ctx)
                except ValueError as e:
                    global_errors.append(str(e))
        return global_errors

    async def enforce_parts(
        self,
        yaml_data: dict[str, Any],
        sections: list[FormSection],
        enforcer_context: dict[str, Any] | None = None,
    ) -> list[str]:
        """Backward compatibility alias for enforce_sections."""
        return await self.enforce_sections(yaml_data, sections, enforcer_context)

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
            if vis.widget == WidgetType.GROUP:
                self.clear_hidden_depends_on(vis.children or [], yaml_data)
                continue
            if not ed.depends_on:
                continue
            if not should_render_editable(vis, yaml_data, siblings=editables):
                smart_delete_value(yaml_data, ed.yaml_path)

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
        resolve_deferrals: bool = True,
        strip_transients: bool = True,
    ) -> dict[str, Any]:
        """
        Write validated form values back into the YAML dict.

        - Deep-copies yaml_data first (preserves original)
        - Skips readonly fields
        - Skips readonly_on_edit fields when edit_mode=True
        - Applies converter.write() before set_value()
        - Optionally resolves defers_to relationships (copy transient → parent)
        - Optionally strips transient fields from output

        Args:
            resolve_deferrals: If True, copy deferred transient values to their parent fields.
                              For wizard steps, set False to preserve both values.
                              For final output, set True to merge them.
            strip_transients: If True, remove transient fields from final output.
                             For wizard steps, set False to keep both values.
                             For final output, set True to clean up.

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
            if not should_render_editable(vis, result, siblings=editables):
                continue

            if vis.widget == WidgetType.GROUP:
                # Groups are transparent — recurse into children
                self.apply_to_yaml(parsed, vis.children or [], result, edit_mode, resolve_deferrals, strip_transients)
            elif vis.widget == WidgetType.SEQUENCE:
                self._apply_sequence_to_yaml(vis, parsed, result, edit_mode)
            elif vis.widget == WidgetType.CHECKBOX:
                # Unchecked checkboxes are absent from form data; treat as False.
                raw = parsed.get(ed.yaml_path)
                value: Any = bool(raw) if raw else False
                if ed.converter:
                    value = _converter_write(ed.converter, value, result)
                if not value and ed.remove_when_none:
                    smart_delete_value(result, ed.yaml_path)
                else:
                    smart_set_value(result, ed.yaml_path, value)
            elif vis.widget == WidgetType.CHECKBOX_GROUP:
                value = _coerce_to_list(parsed.get(ed.yaml_path))
                if ed.converter:
                    value = _converter_write(ed.converter, value, result)
                smart_set_value(result, ed.yaml_path, value)
            else:
                value = parsed.get(ed.yaml_path)
                if value is not None:
                    if ed.converter:
                        value = _converter_write(ed.converter, value, result)
                    if not value and ed.remove_when_none:
                        smart_delete_value(result, ed.yaml_path)
                    else:
                        smart_set_value(result, ed.yaml_path, value)

        if resolve_deferrals:
            self._resolve_deferrals(result, editables)
        if strip_transients:
            self._strip_transients(result, editables)

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
            seq_children = vis.children or []
            for child_vis in seq_children:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                if not should_render_editable(child_vis, yaml_data, siblings=seq_children):
                    continue
                if child_vis.widget == WidgetType.SEQUENCE:
                    self._apply_nested_sequence_to_yaml(child_vis, parsed, yaml_data, edit_mode, parent_index=index)
                elif child_vis.widget == WidgetType.CHECKBOX_GROUP:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = _coerce_to_list(parsed.get(concrete_path))
                    if child_ed.converter:
                        value = _converter_write(child_ed.converter, value, yaml_data)
                    smart_set_value(yaml_data, concrete_path, value)
                else:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = parsed.get(concrete_path)
                    if value is not None:
                        if child_ed.converter:
                            value = _converter_write(child_ed.converter, value, yaml_data)
                        if not value and child_ed.remove_when_none:
                            smart_delete_value(yaml_data, concrete_path)
                        else:
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
                        value = _converter_write(child_ed.converter, value, yaml_data)
                    smart_set_value(yaml_data, child_path, value)

    # ------------------------------------------------------------------
    # JSON submission pipeline (nested structure, no flat intermediate)
    # ------------------------------------------------------------------

    async def process_json_submission(
        self,
        submitted: dict[str, Any],
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        edit_mode: bool = False,
        enforcer_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """Process a nested JSON form submission in a single pass.

        Reads values from the nested JSON using plain path traversal
        (``get_value``), validates them, applies ``converter.write()``,
        and writes to a deep-copy of *yaml_data* using ``smart_set_value``
        (which correctly handles the mixed services list format).

        The key improvement over the flat-key pipeline: **sequence item
        counts come from the submitted data** (the form's truth), not
        from stale session state.

        Args:
            enforcer_context: Optional metadata for enforcers (e.g. project_name).

        Returns:
            ``(result_yaml, errors)`` tuple.
        """
        result = copy.deepcopy(yaml_data)
        errors: dict[str, list[str]] = {}

        for vis in editables:
            ed = vis.editable
            if vis.readonly or (vis.readonly_on_edit and edit_mode):
                continue
            if not should_render_editable(vis, result, siblings=editables):
                continue

            if vis.widget == WidgetType.GROUP:
                await self._process_group_json(
                    vis,
                    submitted,
                    result,
                    errors,
                    edit_mode,
                    enforcer_context,
                )
            elif vis.widget == WidgetType.SEQUENCE:
                self._process_sequence_json(
                    vis,
                    submitted,
                    result,
                    errors,
                    edit_mode,
                )
            elif vis.widget == WidgetType.CHECKBOX:
                # Unchecked checkboxes are absent from JSON; treat as False.
                raw = get_value(submitted, ed.yaml_path)
                value: Any = bool(raw) if raw else False
                self._validate_field(vis, ed.yaml_path, value, errors)
                if ed.converter:
                    value = _converter_write(ed.converter, value, result)
                smart_set_value(result, ed.yaml_path, value)
            elif vis.widget == WidgetType.CHECKBOX_GROUP:
                value = _coerce_to_list(get_value(submitted, ed.yaml_path))
                self._validate_field(vis, ed.yaml_path, value, errors)
                if ed.converter:
                    value = _converter_write(ed.converter, value, result)
                smart_set_value(result, ed.yaml_path, value)
            else:
                value = get_value(submitted, ed.yaml_path)
                self._validate_field(vis, ed.yaml_path, value, errors)
                if value is not None:
                    if ed.converter:
                        value = _converter_write(ed.converter, value, result)
                    smart_set_value(result, ed.yaml_path, value)

        self._resolve_deferrals(result, editables)

        # IMPORTANT: For wizard steps, DON'T strip transient fields.
        # We need BOTH values to persist in wizard state:
        #   - Parent field: "__custom__" (form logic indicator)
        #   - Transient field: "mydomain.nl" (actual value)
        # Only strip transients at final project submission, not during wizard.
        # self._strip_transients(result, editables)

        return result, errors

    async def _process_group_json(
        self,
        vis: EditableVisualizer,
        submitted: dict[str, Any],
        result: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        enforcer_context: dict[str, Any] | None = None,
    ) -> None:
        """Process a group editable: validate children, then run parent enforcer.

        A group is a non-repeating parent that wraps related fields under a
        common path. Unlike sequences, there is no index iteration — children
        are processed directly. The group's enforcer provides cross-field
        validation after all children pass individual validation.
        """
        ed = vis.editable
        errors_before = len(errors)

        # Process each child through the same dispatch logic
        group_children = vis.children or []
        for child_vis in group_children:
            child_ed = child_vis.editable
            if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                continue
            if not should_render_editable(child_vis, result, siblings=group_children):
                continue

            if child_vis.widget == WidgetType.GROUP:
                await self._process_group_json(child_vis, submitted, result, errors, edit_mode, enforcer_context)
            elif child_vis.widget == WidgetType.CHECKBOX:
                raw = get_value(submitted, child_ed.yaml_path)
                value: Any = bool(raw) if raw else False
                self._validate_field(child_vis, child_ed.yaml_path, value, errors)
                if child_ed.converter:
                    value = _converter_write(child_ed.converter, value, result)
                smart_set_value(result, child_ed.yaml_path, value)
            elif child_vis.widget == WidgetType.CHECKBOX_GROUP:
                value = _coerce_to_list(get_value(submitted, child_ed.yaml_path))
                self._validate_field(child_vis, child_ed.yaml_path, value, errors)
                if child_ed.converter:
                    value = _converter_write(child_ed.converter, value, result)
                smart_set_value(result, child_ed.yaml_path, value)
            else:
                value = get_value(submitted, child_ed.yaml_path)
                self._validate_field(child_vis, child_ed.yaml_path, value, errors)
                if value is not None:
                    if child_ed.converter:
                        value = _converter_write(child_ed.converter, value, result)
                    smart_set_value(result, child_ed.yaml_path, value)

        # Run parent enforcer only if children introduced no new errors
        if len(errors) == errors_before and ed.enforcer:
            try:
                await ed.enforcer.enforce(result, enforcer_context or {})
            except ValueError as e:
                errors.setdefault(ed.yaml_path, []).append(str(e))

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

        # Save existing items so we can restore readonly fields after overwriting
        original_items = smart_get_value(result, ed.yaml_path) or []

        # Write the submitted items into result (correct count + raw values)
        smart_set_value(result, ed.yaml_path, copy.deepcopy(items))

        # In edit mode, readonly fields aren't rendered in the form and are
        # therefore absent from the submission.  Restore their values from
        # the original data so they don't get silently dropped.
        if edit_mode and isinstance(original_items, list):
            result_items = smart_get_value(result, ed.yaml_path) or []
            for i in range(min(len(result_items), len(original_items))):
                if not (isinstance(result_items[i], dict) and isinstance(original_items[i], dict)):
                    continue
                for child_vis in vis.children or []:
                    if not (child_vis.readonly or child_vis.readonly_on_edit):
                        continue
                    last_seg = child_vis.editable.yaml_path.rsplit("/", 1)[-1]
                    if last_seg in original_items[i]:
                        result_items[i][last_seg] = copy.deepcopy(original_items[i][last_seg])

        seq_children_json = vis.children or []
        for index in range(len(items)):
            for child_vis in seq_children_json:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                if not should_render_editable(child_vis, result, siblings=seq_children_json):
                    continue
                if child_vis.widget == WidgetType.SEQUENCE:
                    self._process_nested_sequence_json(
                        child_vis,
                        submitted,
                        result,
                        errors,
                        edit_mode,
                        index,
                    )
                elif child_vis.widget == WidgetType.CHECKBOX_GROUP:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = _coerce_to_list(get_value(submitted, concrete_path))
                    self._validate_field(child_vis, concrete_path, value, errors)
                    if child_ed.converter:
                        value = _converter_write(child_ed.converter, value, result)
                    smart_set_value(result, concrete_path, value)
                else:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    value = get_value(submitted, concrete_path)
                    logger.info(
                        "[process_seq_json] child path=%s, value type=%s, value=%r",
                        concrete_path,
                        type(value).__name__,
                        value,
                    )
                    self._validate_field(child_vis, concrete_path, value, errors)
                    if value is not None:
                        if child_ed.converter:
                            value = _converter_write(child_ed.converter, value, result)
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
                        value = _converter_write(child_ed.converter, value, result)
                    smart_set_value(result, child_path, value)

    # ------------------------------------------------------------------
    # Deferral and transient field handling
    # ------------------------------------------------------------------

    def _collect_editables_with_paths(
        self,
        editables: list[EditableVisualizer],
        data: dict[str, Any],
    ) -> list[tuple[Editable, str]]:
        """Collect all editables with their concrete paths.

        For sequence children, resolves wildcard paths to concrete
        indexed paths based on the actual items in data.
        """
        result: list[tuple[Editable, str]] = []
        for vis in editables:
            ed = vis.editable
            if vis.widget == WidgetType.GROUP:
                # Recurse into group children (no index iteration)
                result.extend(self._collect_editables_with_paths(vis.children or [], data))
            elif vis.widget == WidgetType.SEQUENCE:
                items = smart_get_value(data, ed.yaml_path) or []
                if isinstance(items, list):
                    for index in range(len(items)):
                        for child_vis in vis.children or []:
                            child_ed = child_vis.editable
                            concrete = resolve_path(child_ed.yaml_path, index)
                            result.append((child_ed, concrete))
            else:
                result.append((ed, ed.yaml_path))
        return result

    def _resolve_deferrals(
        self,
        data: dict[str, Any],
        editables: list[EditableVisualizer],
    ) -> None:
        """Resolve defers_to relationships: copy transient value to parent path.

        For each editable with ``defers_to`` and ``defer_when``, checks if the
        condition is met on the current value. If so, copies the value from
        the deferred (transient) field path into this editable's path.
        """
        import logging

        logger = logging.getLogger(__name__)

        for ed, concrete_path in self._collect_editables_with_paths(editables, data):
            if not ed.defers_to or not ed.defer_when:
                continue
            current_value = smart_get_value(data, concrete_path)

            # CENTRALIZED DEFERRAL LOGGING
            logger.debug(
                "[deferral check] path=%s, current_value=%s, defers_to=%s",
                concrete_path,
                current_value,
                ed.defers_to,
            )

            if ed.defer_when.check(current_value):
                # Resolve the deferred path with the same index context
                deferred_path = ed.defers_to
                if "[*]" in deferred_path and "[*]" not in concrete_path:
                    # Extract index from concrete_path and apply to deferred_path
                    import re

                    m = re.search(r"\[(\d+)\]", concrete_path)
                    if m:
                        deferred_path = deferred_path.replace("[*]", f"[{m.group(1)}]", 1)
                deferred_value = smart_get_value(data, deferred_path)

                # CENTRALIZED DEFERRAL LOGGING
                logger.debug(
                    "[deferral execute] %s -> %s (deferred_value=%s)",
                    deferred_path,
                    concrete_path,
                    deferred_value,
                )

                if deferred_value is not None:
                    smart_set_value(data, concrete_path, deferred_value)
                    logger.info(
                        "[deferral SUCCESS] copied %s to %s (value=%s)",
                        deferred_path,
                        concrete_path,
                        deferred_value,
                    )
                else:
                    logger.warning(
                        "[deferral SKIPPED] deferred_value is None at path %s",
                        deferred_path,
                    )

    def _restore_sentinel_values(
        self,
        data: dict[str, Any],
        editables: list[EditableVisualizer],
    ) -> None:
        """Restore sentinel values after deferral for wizard state preservation.

        After deferral copies the transient value to the parent, we restore the
        parent back to its sentinel value (e.g., "__custom__") so that:
        1. Form display logic can detect the sentinel
        2. Transient field value can be restored during form rendering
        3. Both values are preserved in wizard state for navigation
        """
        import logging

        logger = logging.getLogger(__name__)

        for ed, concrete_path in self._collect_editables_with_paths(editables, data):
            if not ed.defers_to or not ed.defer_when:
                continue

            # Get the transient field value (what was copied TO the parent)
            deferred_path = ed.defers_to
            transient_value = smart_get_value(data, deferred_path)

            # If the transient has a value, restore parent to sentinel
            if transient_value is not None:
                # Determine the sentinel value (usually "__custom__")
                sentinel = "__custom__"
                if isinstance(ed.defer_when, object) and hasattr(ed.defer_when, "sentinel"):
                    sentinel = ed.defer_when.sentinel

                smart_set_value(data, concrete_path, sentinel)
                logger.debug(
                    "[restore_sentinel] restored %s -> %s (sentinel=%s, kept transient=%s)",
                    concrete_path,
                    sentinel,
                    sentinel,
                    transient_value,
                )

    def _strip_transients(
        self,
        data: dict[str, Any],
        editables: list[EditableVisualizer],
    ) -> None:
        """Remove all transient field values from the output data.

        Transient fields participate in form state but must not persist
        to the final YAML output.
        """
        for ed, concrete_path in self._collect_editables_with_paths(editables, data):
            if ed.transient:
                self._delete_path(data, concrete_path)

    @staticmethod
    def _delete_path(data: dict[str, Any], path: str) -> None:
        """Delete a leaf key from nested data by its path.

        Navigates to the parent container using all segments except the
        last, then pops the leaf key from the parent dict.
        """
        parts = path.split("/")
        if len(parts) == 1:
            data.pop(parts[0], None)
            return
        # Navigate to the parent of the leaf
        parent_path = "/".join(parts[:-1])
        parent = smart_get_value(data, parent_path)
        if isinstance(parent, dict):
            parent.pop(parts[-1], None)

    def propagate_renames(
        self,
        original_data: dict[str, Any],
        result_data: dict[str, Any],
        editables: list[EditableVisualizer],
    ) -> list[str]:
        """Detect renamed fields and cascade changes to all target references.

        Compares old vs new values for editables with ``rename_targets``.
        When a rename is detected, walks each target path (which may contain
        ``[*]`` wildcards) and replaces old_name → new_name in-place.

        Returns:
            List of human-readable rename descriptions for logging.
        """
        renames: list[str] = []
        for ed, concrete_path in self._collect_editables_with_paths(editables, result_data):
            if not ed.rename_targets:
                continue
            old_value = smart_get_value(original_data, concrete_path)
            new_value = smart_get_value(result_data, concrete_path)
            if old_value and new_value and str(old_value) != str(new_value):
                old_name = str(old_value)
                new_name = str(new_value)
                for target_path in ed.rename_targets:
                    count = self._apply_rename(result_data, target_path, old_name, new_name)
                    if count:
                        renames.append(f"{old_name} → {new_name} in {target_path} ({count}x)")
        return renames

    @staticmethod
    def _apply_rename(
        data: dict[str, Any],
        wildcard_path: str,
        old_name: str,
        new_name: str,
    ) -> int:
        """Walk a wildcard path and replace old_name → new_name at leaf positions.

        Handles two kinds of leaf values:
        - String field: ``"frontend"`` → ``"web-app"``
        - List membership: ``["frontend", "backend"]`` → ``["web-app", "backend"]``

        Returns the number of replacements made.
        """
        segments = wildcard_path.split("/")
        count = 0

        def _walk(current: Any, depth: int) -> int:
            nonlocal count
            if depth >= len(segments):
                return count

            seg = segments[depth]
            is_last = depth == len(segments) - 1

            # Parse segment: "key[*]", "key[0]", or plain "key"
            if "[*]" in seg:
                key = seg.replace("[*]", "")
                items = current.get(key) if isinstance(current, dict) else None
                if not isinstance(items, list):
                    return count
                if is_last:
                    # The target IS the list itself — replace members
                    for i, item in enumerate(items):
                        if item == old_name:
                            items[i] = new_name
                            count += 1
                else:
                    for item in items:
                        _walk(item, depth + 1)
            else:
                child = current.get(seg) if isinstance(current, dict) else None
                if child is None:
                    return count
                if is_last:
                    if isinstance(child, str) and child == old_name:
                        current[seg] = new_name
                        count += 1
                    elif isinstance(child, list):
                        for i, item in enumerate(child):
                            if item == old_name:
                                child[i] = new_name
                                count += 1
                else:
                    _walk(child, depth + 1)
            return count

        _walk(data, 0)
        return count

    def populate_deferred_fields(
        self,
        data: dict[str, Any],
        editables: list[EditableVisualizer],
    ) -> None:
        """Prepare data for editing by populating transient fields.

        When loading stored data, a deferred field (e.g. custom domain text
        input) has no stored value. This method detects when the parent's
        stored value triggers the defer condition (via converter.view) and
        copies the stored value into the transient field path so the form
        renders correctly.
        """
        for ed, concrete_path in self._collect_editables_with_paths(editables, data):
            if not ed.defers_to or not ed.defer_when:
                continue
            stored_value = smart_get_value(data, concrete_path)
            if not stored_value:
                continue
            # Apply converter.view to get the display value (e.g. "__custom__")
            display_value = ed.converter.view(stored_value) if ed.converter else stored_value
            if ed.defer_when.check(display_value):
                deferred_path = ed.defers_to
                if "[*]" in deferred_path and "[*]" not in concrete_path:
                    import re

                    m = re.search(r"\[(\d+)\]", concrete_path)
                    if m:
                        deferred_path = deferred_path.replace("[*]", f"[{m.group(1)}]", 1)
                smart_set_value(data, deferred_path, stored_value)
