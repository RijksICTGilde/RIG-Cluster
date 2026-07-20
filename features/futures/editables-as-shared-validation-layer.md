# Future: Editables as Shared Validation Layer

**Status**: To investigate
**Created**: 2026-03-04

## Problem

The form layer and API layer duplicate validation and transformation logic for project YAML mutations. The form layer has proper validators (component name, image, path, env vars, subdomain) while the API layer either skips those checks or uses different logic.

### Current Duplication

| Field | Form validator | API validation |
|-------|---------------|----------------|
| Component name | `ComponentNameValidator` (lowercase, digits, hyphens, max 63 chars) | `sanitize_kubernetes_name()` (different rules, no max length) |
| Container image | `ContainerImageValidator` (lowercase, no spaces) | Only normalizes at write time, no early validation |
| Path | `PathValidator` (starts with `/`, no spaces) | No validation (only `max_length=256`) |
| Env vars | `KeyValueValidator` | No validation (only `max_length=65536`) |
| Subdomain | `SubdomainValidator` | No validation (only `max_length=63`) |
| Base domain | `BaseDomainValidator` | No validation (only `max_length=255`) |

### Validation Gaps in API

API endpoints accept invalid data that only fails later (or not at all):
- Images with spaces or uppercase
- Paths without leading `/`
- Malformed env vars
- Subdomains/base domains in wrong format

## Observation

Editables already define the complete contract for each field:
- **yaml_path** - where data lives in the YAML structure
- **validator** - what's valid
- **converter** - type transformations (e.g., comma-separated string to list)
- **default** - fallback value
- **required** - whether the field must be present

An API mutation needs exactly the same pipeline: parse input, validate, convert, write to YAML path. The difference is just the entry point (JSON vs HTML form), not the business logic.

## What Would Work Naturally

- Validators are standalone - `ComponentNameValidator.validate("frontend")` works without form context
- Converters are bidirectional - work on raw values
- `{K}` and `{F=V}` path filters make even complex writes addressable (e.g., storage config inside mixed service lists)
- `get_value` / `set_value` are pure functions on dicts

## What Needs Investigation

- **Targeting specific items**: Editables use wildcard paths (`components[*]/image`), but API calls target specific items ("update image for component X"). `resolve_path()` and `{F=V}` filters could handle this (`components{name=frontend}/image`), but the ergonomics need design work.
- **Compound operations**: Adding a component sets name + image + ports + services + storage at once. This maps to "apply multiple editables in one batch" - need to define how batching works.
- **Cross-field validation**: Enforcers handle things like "component services must exist in project services". Currently duplicated in `project_manager`. Need to decide where this lives in a shared model.
- **Error format**: Form errors are field-keyed dicts for UI display. API errors need structured JSON responses. The validator output (`list[str]`) could serve both, but the wrapping differs.

## Scope of Impact

### Files with duplicated validation logic
- `opi/api/router.py` - Request models with ad-hoc Field constraints
- `opi/manager/project_manager.py` - `add_component()`, `add_component_to_deployment()`, etc.
- `opi/utils/project_utils.py` - `validate_project_name()`, `normalize_container_image()`, `build_component_config()`

### Files that would provide the shared layer
- `opi/forms/editables/validators.py` - Already has all the validators
- `opi/forms/editables/editable.py` - Field definitions
- `opi/forms/editables/path.py` - YAML path resolution with filter syntax
- `opi/forms/editables/processor.py` - Form data processing pipeline

## Related

- [Unified Service References](../unified-service-references.md) - introduced `{K}` and `{F=V}` path filter syntax that makes this more feasible
