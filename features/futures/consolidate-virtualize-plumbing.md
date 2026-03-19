# Consolidate Virtualize Plumbing

## Problem

The `virtualize` mechanism for service config editables is scattered across 5+ locations that must all agree for values to round-trip correctly:

1. **Editable definition** (`fields/services.py`) -- declares `virtualize=("services", "_services-config")`
2. **`_split_data_across_sections`** (`router_wizard.py`) -- extracts service config into the virtual key when seeding wizard state
3. **`get_merged_data()` devirtualize** (`wizard/state.py`) -- folds virtual keys back into real keys at read time
4. **`template_data` seeding** (`router_detail_edit.py`) -- must include the `services` list so devirtualize has a list to merge into, not a dict
5. **`smart_get_value` / bridge** (`bridge.py`, `service_path.py`) -- reads from real paths, expects `services` as a list

Adding a new flow that touches service config requires remembering to wire up all of these, and forgetting any one of them causes silent data loss or display bugs. The standalone keycloak/postgresql/auth-wall config edit flows all hit this: they lacked `services` in `template_data`, so devirtualize produced a dict instead of a list, and `smart_get_value` returned `None` for every field.

## Desired State

The virtualize/devirtualize round-trip should be self-contained. A single component should own the mapping between virtual form paths and real YAML paths, so that:

- Flow definitions don't need to know about `template_data` seeding
- New standalone config edit flows work without special-casing in `router_detail_edit.py`
- The merge and split logic lives in one place, not spread across router, state, and bridge

## Possible Approaches

### A. WizardState owns the full services list

When `populate_virt_mappings` detects a services virtualize, it automatically snapshots the `services` list from project data into `template_data`. This removes the need for each call site to remember the incantation.

### B. Devirtualize produces the correct structure

Instead of relying on `template_data` to provide the list, `get_merged_data()` could reconstruct the mixed list format from the virtual dict directly. E.g. `{"keycloak": {"config": {...}}}` becomes `[{"keycloak": {"config": {...}}}]`.

### C. Eliminate virtualize entirely

Store service config under the real `services` key in step_data with proper namespacing (e.g. per-section key prefixes) to avoid collisions. This removes the entire virtual/devirtual layer.

## Impact

- `opi/forms/wizard/state.py` -- `get_merged_data`, `populate_virt_mappings`
- `opi/web/router_wizard.py` -- `_split_data_across_sections`
- `opi/web/router_detail_edit.py` -- `modal_wizard_init` template_data seeding
- `opi/forms/visualizers/bridge.py` -- `editable_to_form_field`
- `opi/forms/editables/processor.py` -- `_read_submitted`

## Priority

Medium. The current workaround (adding `services` to `template_data` per flow) works but is fragile. Each new service config flow risks hitting the same bug.
