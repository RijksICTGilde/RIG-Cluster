# Update `add_service` API for schema-version 2

## Context

During the rebase of `claude/editwizard` onto `origin/main`, a conflict arose in `project_utils.py`. Main introduced a reusable `ServiceAdapter.add_services_to_project()` method and a new `POST /api/projects/{name}/services` endpoint. Both use the **old service format** (plain strings in `services` and `uses-services` lists).

Our branch introduced **schema-version 2** service references, where services can be dicts with nested config (e.g. storage configs). The rebase took our v2 inline logic in `project_utils.py`, meaning the auto-merged `add_services_to_project()` method and API endpoint still produce old-format output.

## What needs to happen

1. **`ServiceAdapter.add_services_to_project()`** in `opi/services/services.py` (line ~654):
   - Currently appends plain strings to `project_data["services"]`
   - Should produce v2 service references (dicts with config for storage services, plain strings for others)
   - Storage config logic exists in our `project_utils.py` fallback component builder - extract and reuse

2. **`POST /api/projects/{name}/services`** endpoint in `opi/api/router.py` (line ~1278):
   - Calls `project_manager.add_service()` which delegates to `add_services_to_project()`
   - Should work correctly once the method above is updated
   - Verify response format still makes sense

3. **`ProjectManager.add_service()`** in `opi/manager/project_manager.py` (line ~5460):
   - Thin wrapper around `ServiceAdapter.add_services_to_project()`
   - Likely needs no changes, but verify

## How to verify

- Check that `ServiceAdapter.create_storage_configs()` output matches what v2 expects
- Run existing tests: `uv run pytest tests/integration/test_project_api.py -x -q`
- Test the API endpoint manually with a storage service like `persistent-storage`
- Compare output format with what `project_utils.py` produces for new projects

## Files involved

- `operations-manager/python/opi/services/services.py` - `add_services_to_project()`, `ServiceValidationError`
- `operations-manager/python/opi/api/router.py` - `AddServiceRequest`, `add_service` endpoint
- `operations-manager/python/opi/manager/project_manager.py` - `add_service()` wrapper
- `operations-manager/python/opi/utils/project_utils.py` - reference implementation of v2 service building
