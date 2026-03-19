# 0001 - Remove FormField, use ResolvedEditableVisualizer

**Status**: Proposed
**Date**: 2026-03-09

## Context

The form rendering pipeline currently has an unnecessary intermediate type:

```
EditableVisualizer -> bridge.py -> FormField -> WidgetAdapter -> HTML
```

`FormField` (`opi/forms/field.py`) is a legacy dataclass from the original Pydantic-model-based form system. The bridge (`opi/forms/visualizers/bridge.py`) converts `EditableVisualizer` into `FormField` by resolving values, options, and display logic. This introduces several problems:

1. **Duplication** - `FormField` duplicates fields already on `EditableVisualizer`: label, description, readonly, widget_type, required, placeholder, etc.
2. **Lost context** - the conversion strips editable-specific metadata (converter, depends_on, show_when, defers_to) that downstream code sometimes needs. This caused a bug where `should_render_editable()` couldn't apply the dependency field's converter because `FormField` doesn't carry converter info.
3. **Maintenance overhead** - adding a property to `EditableVisualizer` requires updating `FormField` and the bridge function too.
4. **Dead code** - `FormField` carries unused protocol types (`Converter`, `Validator`) from the old Pydantic form system. The `extractor.py` module that extracts `FormField` from Pydantic models is no longer used by any active code path.

## Decision

Replace `FormField` and the bridge with a `ResolvedEditableVisualizer` - an `EditableVisualizer` enriched with resolved runtime data:

```
EditableVisualizer -> resolve() -> ResolvedEditableVisualizer -> WidgetAdapter -> HTML
```

`ResolvedEditableVisualizer` adds only the fields that require runtime resolution:
- `value` - resolved from YAML data with converter applied
- `options` - resolved from the options provider
- `errors` - validation errors for this field
- `concrete_path` - the resolved path (wildcards replaced with indices)

The `WidgetAdapter` base class and `ROOSWidgetAdapter` are updated to accept `ResolvedEditableVisualizer` instead of `FormField`.

### Files to remove
- `opi/forms/field.py` (FormField dataclass)
- `opi/forms/visualizers/bridge.py` (`editable_to_form_field` function)
- `opi/forms/extractor.py` (Pydantic model extraction, unused)
- `opi/forms/schema.py` (FormMeta, if only used by extractor)

### Files to update
- `opi/forms/widgets/base.py` - accept ResolvedEditableVisualizer
- `opi/forms/widgets/roos.py` - accept ResolvedEditableVisualizer
- `opi/forms/renderer.py` - call resolve() instead of bridge
- Widget templates (`templates/widgets/*.html.j2`) - use new field names
- `should_render_editable()` - move to editables layer (no longer needs bridge)

## Consequences

**Easier:**
- Adding new editable properties flows through to rendering automatically
- Visibility checks have full access to editable metadata (converters, conditions)
- Fewer files to maintain, clearer data flow
- No more confusion about which type carries which data

**Harder:**
- One-time migration effort across widget templates and adapter methods
- Widget templates need to use `EditableVisualizer`/`Editable` attribute names instead of `FormField` names (e.g. `field.editable.yaml_path` vs `field.path`)

**Risks:**
- Widget templates are tightly coupled to `FormField` attribute names - migration must be thorough
- The `WidgetAdapter` abstract base assumes a single UI framework swap is possible; this remains true with the new type

## Related

- Feature doc: `features/editable-driven-project-forms.md` (section "Known Technical Debt: The Bridge and FormField")
- Bug fix: `should_render_editable()` needed sibling converter lookup because `FormField` strips converter context
