# Processor Dispatch Consolidation

## Problem

`opi/forms/editables/processor.py` contains heavily duplicated dispatch logic. The same pattern - check widget type, extract value, validate, apply converter, write to YAML - is repeated across seven methods:

- `apply_to_yaml` (flat form pipeline)
- `_apply_sequence_to_yaml`
- `_apply_nested_sequence_to_yaml`
- `process_json_submission` (JSON pipeline)
- `_process_group_json`
- `_process_sequence_json`
- `_process_nested_sequence_json`

Each method has near-identical `if/elif/else` blocks for `CHECKBOX`, `CHECKBOX_GROUP`, `SEQUENCE`, `GROUP`, and the default text case. Any change to the dispatch logic (e.g. passing context to `converter.write()`) must be applied ~15 times.

## Impact

- High maintenance cost: every converter/validator protocol change touches many places
- Easy to miss a call site, causing inconsistent behavior
- The flat (`apply_to_yaml`) and JSON (`process_json_submission`) pipelines duplicate logic with slightly different data access patterns (`parsed.get()` vs `get_value()`)

## Proposed Solution

Extract a single `_process_field(vis, value, result, errors, yaml_data)` helper that handles:
1. Validation
2. Converter write (with yaml_data context)
3. Writing to result dict

The sequence/group/nested methods would iterate and delegate to this helper instead of inlining the full dispatch. The flat and JSON pipelines differ only in how they read values - this can be abstracted with a value-getter callback.

## Literal Scalar Marker on Editable

`_apply_literal_scalars()` in `router_wizard.py` hardcodes which fields need `LiteralScalarString` formatting (config keys, repo passwords, user-env-vars). This is fragile - adding a new encrypted field means updating that function.

The fix: add `literal_scalar: bool = False` to `Editable`. Fields that produce multiline/encrypted values (like `user-env-vars`, `age-private-key`) set it to `True`. The save path walks all editables and applies `LiteralScalarString` generically, replacing `_apply_literal_scalars()` entirely.

## Scope

- Refactor internal to `processor.py` - no API changes
- All existing tests should pass unchanged
- Consider removing the flat pipeline (`apply_to_yaml`) if it's no longer used, or unifying it with the JSON pipeline
- Replace `_apply_literal_scalars()` with editable-driven literal scalar marking
