# Plan: Assign Existing Service to Component Endpoint

## Context
The `edit-services-over-api` branch added `POST /projects/{project_name}/services` to add a new service to a project (and optionally assign it to components). Now we need a complementary endpoint that assigns an **already-existing** project-level service to an existing component — without re-adding it to the project-level services list.

## Route
`POST /projects/{project_name}/components/{component_name}/services`

Body: `{ "services": ["postgresql-database"] }`

## Implementation Steps

### 1. Extract shared helper in `ServiceAdapter` (services.py:700-741)
Extract the component-update logic (updating `uses-services` + storage configs) from `add_services_to_project()` into a private helper:

**New method: `_assign_services_to_components(project_data, service_names, component_names) -> list[str]`**
- Validates component names exist
- Updates each component's `uses-services` list
- Adds storage configs for storage services
- Returns list of updated component names

Then **refactor `add_services_to_project()`** (lines 700-741) to call this helper instead of inlining the logic.

### 2. New public method in `ServiceAdapter` (services.py)
**`assign_services_to_component(project_data, service_names, component_name) -> dict`**
- Validates service names are known (`parse_services_from_strings`)
- Resolves dependencies (`resolve_service_dependencies`)
- Validates all resolved services exist at project level (unlike `add_services_to_project` which adds them)
- Determines which are already on the component (→ `services_skipped` + warnings)
- Delegates to `_assign_services_to_components()` for the actual mutation
- Returns `{ services_assigned, services_skipped, components_updated, warnings }`

### 3. New method in `ProjectManager` (project_manager.py, after `add_service` ~line 5500)
**`assign_service_to_component(component_name, service_names) -> dict`**
- Same orchestration pattern as `add_service()`: get contents → call ServiceAdapter → save + commit if changed
- Commit message: `"Assign service(s) X to component 'Y' in project 'Z'"`
- Maps `ServiceValidationError` to appropriate `error_type` values:
  - `"service_not_on_project"` — service not at project level
  - `"component_not_found"` — component doesn't exist
  - `"invalid_service"` — unknown service name

### 4. New Pydantic model in router.py (after `AddServiceRequest` ~line 840)
```python
class AssignServiceToComponentRequest(BaseModel):
    services: list[str] = Field(..., min_length=1, description="Service name(s) to assign (must already exist at project level)")
```

### 5. New endpoint in router.py (after `add_service` endpoint ~line 1383)
**`POST /projects/{project_name}/components/{component_name}/services`**
- `@validate_api_token` decorator
- Validate project name (`validate_project_name`)
- Validate component name format (`sanitize_kubernetes_name` — already imported)
- Call `project_manager.assign_service_to_component()`
- Process all deployments when `services_assigned` is non-empty (same pattern as `add_service`)
- Error status codes: `service_not_on_project` → 400, `component_not_found` → 404, `invalid_service` → 400
- Returns 201 on success

### 6. Integration tests (test_project_api.py)
- Update `create_mock_project_manager()` with `assign_service_to_component_result` parameter
- New test class `TestAssignServiceToComponentEndpoint` with tests for:
  - Success (service assigned, processing runs)
  - Service already assigned to component (201 with warnings, processing skipped)
  - Service not on project (400)
  - Unknown service name (400)
  - Component not found (404)
  - Missing/empty services field (422)
  - Auth: no API key (401), invalid API key (401)

## Files to Modify
1. `operations-manager/python/opi/services/services.py` — extract helper + new public method
2. `operations-manager/python/opi/manager/project_manager.py` — new orchestration method
3. `operations-manager/python/opi/api/router.py` — Pydantic model + endpoint
4. `operations-manager/python/tests/integration/test_project_api.py` — mock helper + tests

## Verification
1. Run existing tests to confirm the refactoring of `add_services_to_project()` causes no regressions: `uv run pytest tests/ -k "test_add_service"`
2. Run new tests: `uv run pytest tests/ -k "test_assign_service"`
3. Run full test suite: `uv run pytest`
