"""
Form data processor for the editables pipeline.

Handles the reverse flow: form submission -> validation -> YAML update.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from opi.forms.editables.converters import keep_existing_ciphertext_if_unchanged
from opi.forms.editables.editable import WidgetType, apply_virtualize
from opi.forms.editables.path import get_value, resolve_path
from opi.forms.editables.service_path import (
    is_service_config_path,
    smart_delete_value,
    smart_get_value,
    smart_set_value,
)
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


def _match_original_item(item: Any, originals: list[Any], index: int) -> Any:
    """Find the pre-edit item matching a submitted sequence item.

    Matches on a stable identity key (``reference`` or ``name``) so add / remove /
    reorder are handled; falls back to positional index.
    """
    if isinstance(item, dict):
        for key in ("reference", "name"):
            ident = item.get(key)
            if ident:
                for orig in originals:
                    if isinstance(orig, dict) and orig.get(key) == ident:
                        return orig
    if 0 <= index < len(originals):
        return originals[index]
    return None


def _prune_paths(item: dict[str, Any], rel_paths: list[str]) -> dict[str, Any]:
    """Remove the given ``/``-separated relative paths from a (copied) item.

    Used to drop the fields a sequence section actually manages, so the rest of the
    original item can be merged back in without re-introducing user-removed values.
    """
    for rel in rel_paths:
        segs = [s for s in rel.split("/") if s and "[" not in s]
        if not segs:
            continue
        node: Any = item
        for seg in segs[:-1]:
            if isinstance(node, dict) and seg in node:
                node = node[seg]
            else:
                node = None
                break
        if isinstance(node, dict):
            node.pop(segs[-1], None)
    return item


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overlay`` into ``base`` in place; overlay wins on conflicts."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _prune_empty_ancestors(data: dict[str, Any], path: str) -> None:
    """After deleting a leaf, remove now-empty ancestor dicts (noise like ``config: {}``).

    Walks up the path removing each dict key whose value became an empty dict, stopping
    at the first non-empty container or a list-item segment (``...[N]``). Skipped for
    service-config and service-map (``{...}``) paths, where an empty entry is meaningful
    (it marks a selected service in the base-component services list).
    """
    if is_service_config_path(path) or "{" in path:
        return
    parts = path.split("/")
    while len(parts) > 1:
        parts = parts[:-1]
        if parts[-1].endswith("]"):
            break
        parent_path = "/".join(parts)
        node = smart_get_value(data, parent_path)
        if isinstance(node, dict) and not node:
            smart_delete_value(data, parent_path)
        else:
            break


if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.visualizers.visualizer import EditableVisualizer


def _converter_write(converter: Any, value: Any, context_data: dict[str, Any] | None = None) -> Any:
    """Call converter.write() with context data."""
    return converter.write(value, context_data=context_data)


def _read_submitted(submitted: dict[str, Any], ed: Editable) -> Any:
    """Read a field value from submitted data, respecting virtualize.

    Uses the list-aware ``smart_get_value`` (not plain ``get_value``) so that
    reads stay symmetric with the writes in ``_write_field`` (which use
    ``smart_set_value``). This matters on the final wizard pass, where the
    submitted data is ``get_merged_data()`` with ``services`` devirtualized
    back into a native list: a plain key like ``services/keycloak/...`` cannot
    be traversed with ``get_value``, so a checkbox would read ``None`` ->
    ``False`` and ``remove_when_none`` would silently delete it.
    """
    virt = ed.virtualize
    read_path = apply_virtualize(ed.yaml_path, virt) if virt else ed.yaml_path
    value = smart_get_value(submitted, read_path)
    if value is None and virt and read_path != ed.yaml_path:
        value = smart_get_value(submitted, ed.yaml_path)
    return value


class EditableFormProcessor:
    """Processes form submissions through the editables pipeline."""

    def __init__(self) -> None:
        self.field_warnings: dict[str, list[str]] = {}

    @staticmethod
    def _validate_field(
        vis: EditableVisualizer,
        path: str,
        value: Any,
        errors: dict[str, list[str]],
        context: dict[str, Any] | None = None,
    ) -> None:
        """Validate a single field for required and custom validator rules."""
        ed = vis.editable
        if ed.required and not value:
            errors.setdefault(path, []).append("Dit veld is verplicht")
            return
        if ed.validator:
            try:
                field_errors = ed.validator.validate(value, context=context or {})
            except TypeError:
                field_errors = ed.validator.validate(value)
            if field_errors:
                errors.setdefault(path, []).extend(field_errors)

    async def enforce_sections(
        self,
        yaml_data: dict[str, Any],
        sections: list[FormSection],
        enforcer_context: dict[str, Any] | None = None,
        field_errors: dict[str, list[str]] | None = None,
        field_warnings: dict[str, list[str]] | None = None,
    ) -> list[str]:
        """
        Run section-level enforcers.

        Args:
            enforcer_context: Optional metadata for enforcers (e.g. project_name).
            field_errors: When provided, ``FieldError`` exceptions are
                merged into this dict (keyed by field path) instead of
                appearing in the returned global errors list.
            field_warnings: When provided, ``FieldWarning`` exceptions are
                merged into this dict (keyed by field path). Warnings do
                not block submission.

        Returns:
            List of global error messages. Empty means all passed.
        """
        from opi.forms.editables.enforcers import FieldError, FieldWarning

        ctx = enforcer_context or {}
        global_errors: list[str] = []
        for section in sections:
            if section.enforcer:
                try:
                    await section.enforcer.enforce(yaml_data, ctx)
                except FieldWarning as e:
                    if field_warnings is not None:
                        field_warnings.setdefault(e.field_path, []).append(str(e))
                except FieldError as e:
                    if field_errors is not None:
                        field_errors.setdefault(e.field_path, []).append(str(e))
                    else:
                        global_errors.append(str(e))
                except ValueError as e:
                    global_errors.append(str(e))
        return global_errors

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

    def apply_dependent_generators(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
    ) -> None:
        """Run generators for editables that have both depends_on and a generator.

        These are computed fields whose value is derived from another field
        (e.g., issuer derived from base-domain). Mutates ``yaml_data`` in
        place. When the generated value is falsy and ``remove_when_none``
        is set, the key is removed from the YAML.

        Recurses into GROUP widgets to find nested computed editables.
        Also walks the editable children tree directly, so computed fields
        defined in an editable group (without a visualizer) are picked up.
        """
        for vis in editables:
            ed = vis.editable
            if vis.widget == WidgetType.GROUP:
                self.apply_dependent_generators(vis.children or [], yaml_data)
                # Also check editable children that have no visualizer counterpart
                self._apply_editable_generators(ed.children or [], yaml_data)
                continue
            if not ed.generator or not ed.depends_on:
                continue
            self._run_generator(ed, yaml_data)

    def _apply_editable_generators(
        self,
        editables: list[Editable],
        yaml_data: dict[str, Any],
    ) -> None:
        """Walk raw editable children for dependent generators."""
        for ed in editables:
            if ed.children:
                self._apply_editable_generators(ed.children, yaml_data)
            if not ed.generator or not ed.depends_on:
                continue
            self._run_generator(ed, yaml_data)

    @staticmethod
    def _run_generator(ed: Editable, yaml_data: dict[str, Any]) -> None:
        """Execute a single dependent generator and write the result."""
        assert ed.generator is not None  # noqa: S101 - caller guarantees this
        value = ed.generator.generate(yaml_data)
        if not value and ed.remove_when_none:
            smart_delete_value(yaml_data, ed.yaml_path)
        elif value is not None:
            smart_set_value(yaml_data, ed.yaml_path, value)

    # ------------------------------------------------------------------
    # JSON submission pipeline (nested structure, no flat intermediate)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_field(
        editable: Editable,
        path: str,
        value: Any,
        data: dict[str, Any],
    ) -> None:
        """Convert and write a single field value, respecting remove_when_none.

        Centralises the convert-then-write logic so ``remove_when_none``,
        converter dispatch, and ``smart_set_value`` / ``smart_delete_value``
        live in exactly one place.

        When a converter produces a fresh AGE-encrypted value whose plaintext
        matches the stored ciphertext (forms post decrypted values back, so
        untouched fields re-encrypt to different random ciphertext), the
        stored ciphertext is kept verbatim to avoid churn in the project file.
        """
        if value is None:
            return
        if editable.converter:
            existing = smart_get_value(data, path)
            value = _converter_write(editable.converter, value, data)
            value = keep_existing_ciphertext_if_unchanged(existing, value, data)
        if not value and editable.remove_when_none:
            smart_delete_value(data, path)
            _prune_empty_ancestors(data, path)
        else:
            smart_set_value(data, path, value)

    async def process_json_submission(
        self,
        submitted: dict[str, Any],
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        edit_mode: bool = False,
        enforcer_context: dict[str, Any] | None = None,
        strip_transients: bool = False,
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """Process a nested JSON form submission in a single pass.

        Reads values from the nested JSON using plain path traversal
        (``get_value``), validates them, applies ``converter.write()``,
        and writes to a deep-copy of *yaml_data* using ``smart_set_value``
        (which correctly handles the mixed services list format).

        Sequence item counts come from the submitted data (the form's
        truth), not from stale session state.

        Args:
            enforcer_context: Optional metadata for enforcers (e.g. project_name).
            strip_transients: When True, remove transient fields from the
                output. Use for final project submission; leave False for
                wizard steps where both parent and transient values must
                persist in session state.

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
                # When ``show_when`` no longer holds against the in-progress
                # ``result`` the field is conceptually hidden; processing it
                # anyway would let stale form data clobber the now-current
                # state. The dependency-providing editable is expected to
                # appear earlier in this list (and has therefore already
                # written its new value into ``result``), so this check
                # reflects the user's latest intent — matching the strict
                # skip that ``_process_sequence_json`` already does at
                # line 461 for in-sequence children.
                continue

            if vis.widget == WidgetType.GROUP:
                await self._process_group_json(
                    vis,
                    submitted,
                    result,
                    errors,
                    edit_mode,
                    enforcer_context,
                    warnings=self.field_warnings,
                )
            elif vis.widget == WidgetType.SEQUENCE:
                self._process_sequence_json(
                    vis,
                    submitted,
                    result,
                    errors,
                    edit_mode,
                    enforcer_context,
                )
            elif vis.widget == WidgetType.CHECKBOX:
                raw = _read_submitted(submitted, ed)
                value: Any = bool(raw) if raw else False
                self._validate_field(vis, ed.yaml_path, value, errors, enforcer_context)
                self._write_field(ed, ed.yaml_path, value, result)
            elif vis.widget == WidgetType.CHECKBOX_GROUP:
                value = _coerce_to_list(_read_submitted(submitted, ed))
                self._validate_field(vis, ed.yaml_path, value, errors, enforcer_context)
                self._write_field(ed, ed.yaml_path, value, result)
            else:
                value = _read_submitted(submitted, ed)
                self._validate_field(vis, ed.yaml_path, value, errors, enforcer_context)
                self._write_field(ed, ed.yaml_path, value, result)

        self._resolve_deferrals(result, editables)

        if strip_transients:
            self.strip_transients_from(result, editables)

        return result, errors

    async def _process_group_json(
        self,
        vis: EditableVisualizer,
        submitted: dict[str, Any],
        result: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        enforcer_context: dict[str, Any] | None = None,
        warnings: dict[str, list[str]] | None = None,
    ) -> None:
        """Process a group editable: validate children, then run parent enforcer.

        A group is a non-repeating parent that wraps related fields under a
        common path. Unlike sequences, there is no index iteration - children
        are processed directly. The group's enforcer provides cross-field
        validation after all children pass individual validation.
        """
        from opi.forms.editables.enforcers import FieldError, FieldWarning

        ed = vis.editable
        errors_before = len(errors)
        if warnings is None:
            warnings = {}

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
                raw = _read_submitted(submitted, child_ed)
                value: Any = bool(raw) if raw else False
                self._validate_field(child_vis, child_ed.yaml_path, value, errors, enforcer_context)
                self._write_field(child_ed, child_ed.yaml_path, value, result)
            elif child_vis.widget == WidgetType.CHECKBOX_GROUP:
                value = _coerce_to_list(_read_submitted(submitted, child_ed))
                self._validate_field(child_vis, child_ed.yaml_path, value, errors, enforcer_context)
                self._write_field(child_ed, child_ed.yaml_path, value, result)
            else:
                value = _read_submitted(submitted, child_ed)
                self._validate_field(child_vis, child_ed.yaml_path, value, errors, enforcer_context)
                self._write_field(child_ed, child_ed.yaml_path, value, result)

        # Always run enforcer: warnings are collected regardless of child errors,
        # but errors are only propagated when children have no errors.
        has_child_errors = len(errors) > errors_before
        if ed.enforcer:
            try:
                await ed.enforcer.enforce(result, enforcer_context or {})
            except FieldWarning as e:
                warnings.setdefault(e.field_path, []).append(str(e))
            except FieldError as e:
                if not has_child_errors:
                    errors.setdefault(e.field_path, []).append(str(e))
            except ValueError as e:
                if not has_child_errors:
                    errors.setdefault(ed.yaml_path, []).append(str(e))

    def _process_sequence_json(
        self,
        vis: EditableVisualizer,
        submitted: dict[str, Any],
        result: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Process a sequence editable from nested JSON.

        Reads the items array from ``submitted`` (source of truth for
        item count), deep-copies it into ``result``, then validates
        and applies converters for each child editable.
        """
        ed = vis.editable
        virt = ed.virtualize
        read_path = apply_virtualize(ed.yaml_path, virt) if virt else ed.yaml_path
        items = get_value(submitted, read_path)
        if not isinstance(items, list) and virt and read_path != ed.yaml_path:
            items = get_value(submitted, ed.yaml_path)
        if not isinstance(items, list):
            items = []

        # Empty sequence + remove_when_none: don't persist an empty list (e.g. an
        # attachments coupling with no entries). For a plain path this removes the key;
        # skipping the write below avoids writing a fresh empty list.
        if not items and ed.remove_when_none:
            smart_delete_value(result, ed.yaml_path)
            return

        # Save existing items so we can restore readonly fields after overwriting
        original_items = smart_get_value(result, ed.yaml_path) or []

        # Write the submitted items into result. Each submitted item is merged over its
        # pre-edit counterpart (matched by reference/name, else index) so fields this
        # section does NOT manage (e.g. image, the service-revision map, sibling
        # services) are preserved instead of being clobbered by the overwrite. Managed
        # fields are pruned from the original first, so user-removed values are not
        # re-introduced; the per-child processing below sets the managed values.
        prefix = f"{ed.yaml_path}[*]/"
        managed_rel = [
            child.editable.yaml_path.removeprefix(prefix)
            for child in (vis.children or [])
            if not (child.readonly or (child.readonly_on_edit and edit_mode))
            and child.editable.yaml_path.startswith(prefix)
        ]
        merged_items: list[Any] = []
        originals = original_items if isinstance(original_items, list) else []
        for idx, item in enumerate(items):
            orig = _match_original_item(item, originals, idx)
            if isinstance(item, dict) and isinstance(orig, dict):
                base = _prune_paths(copy.deepcopy(orig), managed_rel)
                merged_items.append(_deep_merge(base, copy.deepcopy(item)))
            else:
                merged_items.append(copy.deepcopy(item))
        smart_set_value(result, ed.yaml_path, merged_items)

        # Strip the virtual key from result so it doesn't leak into YAML
        if virt and read_path != ed.yaml_path:
            virtual_key = virt[1]
            virtual_parts = read_path.split("/")
            parent_parts = []
            for part in virtual_parts:
                if part.startswith(virtual_key):
                    break
                parent_parts.append(part)
            parent_path = "/".join(parent_parts)
            parent = smart_get_value(result, parent_path) if parent_path else result
            if isinstance(parent, dict):
                parent.pop(virtual_key, None)

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

        # When the sequence itself declares ``virtualize`` (e.g.
        # PERSISTENT_STORAGE_SEQUENCE in the modal-edit-component flow where
        # it is flattened to the top level), propagate that mapping to each
        # non-sequence child so the form submission is read from the virtual
        # ``_services-config{…}`` path rather than the real ``services{…}``
        # one — which would collide with the sibling service-selection list.
        seq_virt = ed.virtualize

        seq_children_json = vis.children or []
        for index in range(len(items)):
            for child_vis in seq_children_json:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                if not should_render_editable(child_vis, result, index=index, siblings=seq_children_json):
                    continue
                if child_vis.widget == WidgetType.SEQUENCE:
                    self._process_nested_sequence_json(
                        child_vis,
                        submitted,
                        result,
                        errors,
                        edit_mode,
                        index,
                        context,
                    )
                elif child_vis.widget == WidgetType.CHECKBOX_GROUP:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    virt = child_ed.virtualize or seq_virt
                    read_path = apply_virtualize(concrete_path, virt) if virt else concrete_path
                    value = _coerce_to_list(get_value(submitted, read_path))
                    if not value and virt and read_path != concrete_path:
                        value = _coerce_to_list(get_value(submitted, concrete_path))
                    self._validate_field(child_vis, concrete_path, value, errors, context)
                    self._write_field(child_ed, concrete_path, value, result)
                else:
                    concrete_path = resolve_path(child_ed.yaml_path, index)
                    virt = child_ed.virtualize or seq_virt
                    read_path = apply_virtualize(concrete_path, virt) if virt else concrete_path
                    value = get_value(submitted, read_path)
                    # Fall back to real path when merged data has no virtual key
                    if value is None and virt and read_path != concrete_path:
                        value = get_value(submitted, concrete_path)
                    self._validate_field(child_vis, concrete_path, value, errors, context)
                    self._write_field(child_ed, concrete_path, value, result)
                    # Clean up virtual key from result
                    if virt and read_path != concrete_path:
                        virtual_key = virt[1]
                        virtual_parts = read_path.split("/")
                        parent_parts = []
                        for part in virtual_parts:
                            if part.startswith(virtual_key):
                                break
                            parent_parts.append(part)
                        parent_path = "/".join(parent_parts)
                        parent = smart_get_value(result, parent_path) if parent_path else result
                        if isinstance(parent, dict):
                            parent.pop(virtual_key, None)

    def _process_nested_sequence_json(
        self,
        vis: EditableVisualizer,
        submitted: dict[str, Any],
        result: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        parent_index: int,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Process a nested sequence from JSON (e.g. additional-clients[0]/redirect-uris).

        When the editable has a ``virtualize`` mapping, form field names use a
        virtual path segment (e.g. ``_services-config`` instead of ``services``)
        to avoid collisions with sibling fields that share the real path prefix.
        Submitted data is read from the virtual path; validated and written to
        the real path in the result.
        """
        ed = vis.editable
        virt = ed.virtualize
        real_seq_path = resolve_path(ed.yaml_path, parent_index)
        virtual_seq_path = apply_virtualize(real_seq_path, virt) if virt else real_seq_path

        # Read from virtual path in submitted data.  When virtualize is
        # active the form POSTs under the virtual key (e.g. _services-config).
        # However, after a step-level submit the data is already merged into
        # the real path (services{…}/config).  During the final submit the
        # merged data no longer has the virtual key, so we must fall back to
        # reading from the real path to avoid overwriting good data with [].
        items = get_value(submitted, virtual_seq_path)
        if not isinstance(items, list) and virt:
            items = get_value(submitted, real_seq_path)
        if not isinstance(items, list):
            items = []

        # Write to real path in result
        smart_set_value(result, real_seq_path, copy.deepcopy(items))

        # Strip the virtual key (e.g. _services-config) from result so it
        # does not leak into step_data or the final project YAML.
        # We delete the entire virtual container key from the parent, not
        # just the leaf - otherwise empty dicts remain.
        # The virtual key lives under the component dict, so we find the
        # path segment that starts with the virtual key name and take
        # everything before it as the parent path.
        if virt and virtual_seq_path != real_seq_path:
            virtual_key = virt[1]  # e.g. "_services-config"
            virtual_parts = virtual_seq_path.split("/")
            parent_parts = []
            for part in virtual_parts:
                if part.startswith(virtual_key):
                    break
                parent_parts.append(part)
            parent_path = "/".join(parent_parts)
            parent = smart_get_value(result, parent_path) if parent_path else result
            if isinstance(parent, dict):
                parent.pop(virtual_key, None)

        for child_index in range(len(items)):
            for child_vis in vis.children or []:
                child_ed = child_vis.editable
                if child_vis.readonly or (child_vis.readonly_on_edit and edit_mode):
                    continue
                # Real path for validation + writing
                real_child_path = resolve_path(child_ed.yaml_path, parent_index)
                real_child_path = resolve_path(real_child_path, child_index)
                # Virtual path for reading from submitted data
                virtual_child_path = apply_virtualize(real_child_path, virt) if virt else real_child_path
                value = get_value(submitted, virtual_child_path)
                # Fall back to real path if virtual path has no data
                if value is None and virt:
                    value = get_value(submitted, real_child_path)
                self._validate_field(child_vis, real_child_path, value, errors, context)
                self._write_field(child_ed, real_child_path, value, result)

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

    def strip_transients_from(
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
                    # The target IS the list itself - replace members
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
            display_value = ed.converter.view(stored_value, context_data=data) if ed.converter else stored_value
            if ed.defer_when.check(display_value):
                deferred_path = ed.defers_to
                if "[*]" in deferred_path and "[*]" not in concrete_path:
                    import re

                    m = re.search(r"\[(\d+)\]", concrete_path)
                    if m:
                        deferred_path = deferred_path.replace("[*]", f"[{m.group(1)}]", 1)
                smart_set_value(data, deferred_path, stored_value)
