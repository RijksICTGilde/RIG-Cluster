# API V2 Async Migration Plan

**Status:** Planning
**Created:** 2026-03-02
**Objective:** Migrate long-running API operations to async task system while maintaining backward compatibility

## Overview

Currently, the Operations Manager API has synchronous endpoints that block until operations complete. We're introducing a V2 API with true async behavior using the async task system that was recently implemented.

**Key Design:**
- **V1 (current unversioned endpoints):** Keep working as-is for backward compatibility
  - `/api/projects/...`, `/api/deployments/...`, etc.
  - Internally use async tasks but **block and wait** for completion
  - Clients see no change

- **V2 (new versioned endpoints):** True async/fire-and-forget
  - `/api/v2/projects/...`, `/api/v2/deployments/...`, etc.
  - Return immediately with `202 Accepted` + task ID
  - Clients must poll `/api/tasks/{task_id}` for status

- **Single docs endpoint:** `/docs` shows both v1 (deprecated) and v2 (current) with clear tags

## Architecture

```
operations-manager/python/opi/
├── core/
│   ├── task_helpers.py                 # NEW: Shared task utilities
│   ├── async_task_service.py           # EXISTING: Task management
│   └── task_worker.py                  # EXISTING: Task execution
├── api/
│   ├── router.py                       # EXISTING: Main router (will include v2)
│   ├── v2/                             # NEW: V2 endpoints
│   │   ├── __init__.py
│   │   ├── router.py                   # V2 endpoint implementations
│   │   ├── models.py                   # V2 request/response models
│   │   └── dependencies.py             # V2-specific dependencies
│   └── [existing v1 files]             # UNCHANGED: Keep as-is
```

## Long-Running Operations to Migrate

These operations will have both V1 (blocking) and V2 (async) versions:

1. **upsert_deployment** - Create/update deployment
2. **create_project** - Create new project
3. **delete_deployment** - Delete a deployment
4. **update_image** - Update component image
5. **clone_database** - Clone database from source
6. **clone_bucket** - Clone S3 bucket from source
7. **refresh_deployment** - Refresh deployment from git

Quick operations that stay synchronous only:
- List/get projects and deployments
- Get project/deployment details
- Add registry credentials
- Domain settings
- Validation endpoints

## Implementation Steps

### Phase 1: Foundation (Shared Task Helpers)

**File: `opi/core/task_helpers.py`**

Create shared utilities used by both V1 and V2:

```python
async def create_async_task(
    task_type: str,
    project_name: str,
    deployment_name: str | None = None,
    payload: dict | None = None,
    cluster: str = "local",
) -> dict:
    """Create task in database, return task dict with id and status."""
    # Implementation details in code

async def wait_for_task_completion(
    task_id: str,
    timeout_seconds: int = 1800,  # 30 minutes
    poll_interval: float = 0.5,
    cluster: str = "local",
) -> dict:
    """Poll task until completion (used by v1 for blocking behavior)."""
    # Implementation details in code

async def get_task_result(task_id: str, cluster: str = "local") -> dict:
    """Get completed task result and metadata."""
    # Implementation details in code
```

**Responsibilities:**
- Abstract task creation from API logic
- Handle polling with timeout
- Provide common error handling

### Phase 2: V2 Models and Response Formats

**File: `opi/api/v2/models.py`**

Create response models for V2 endpoints:

```python
# Async task acceptance response (202)
class AsyncTaskAcceptedResponse(BaseModel):
    status: str = "accepted"
    task_id: str
    task_type: str
    poll_url: str  # e.g., "/api/tasks/550e8400-..."

# Result models for different operation types
class UpsertDeploymentResult(BaseModel):
    deployment_name: str
    web_addresses: list[str]
    warnings: list[str] = []

class CreateProjectResult(BaseModel):
    project_name: str
    api_key: str

class DeleteDeploymentResult(BaseModel):
    deployment_name: str
    resources_removed: list[str]

class UpdateImageResult(BaseModel):
    deployment_name: str
    image: str
    previous_image: str

# All results stored in task.result as dict[str, Any]
```

### Phase 3: V2 Endpoints Router

**File: `opi/api/v2/router.py`**

Create V2 endpoints that use async tasks:

```python
@router.post(
    "/projects/{project_name}/:upsert-deployment",
    tags=["v2", "deployments"],
    responses={
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    }
)
async def upsert_deployment_v2(
    request: Request,
    project_name: str,
    deployment_data: UpsertDeploymentRequest = Body(...),
) -> JSONResponse:
    """Create or update deployment (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.
    """
    # Create task (don't wait)
    task = await create_async_task(
        task_type="upsert_deployment",
        project_name=project_name,
        deployment_name=deployment_data.deploymentName,
        payload={...},
    )

    # Return immediately with 202
    return JSONResponse(
        content={
            "status": "accepted",
            "task_id": task["task_id"],
            "task_type": "upsert_deployment",
            "poll_url": f"/api/tasks/{task['task_id']}",
        },
        status_code=202,
        headers={"Location": f"/api/tasks/{task['task_id']}"}
    )
```

**Pattern:** All V2 endpoints follow this:
1. Validate input
2. Create async task
3. Return 202 + task ID immediately
4. **Don't wait for completion**

### Phase 4: Refactor V1 Endpoints

**File: `opi/api/router.py`** (modify existing endpoints)

Update existing endpoints to use async tasks internally:

```python
@router.post(
    "/projects/{project_name}/:upsert-deployment",
    tags=["v1 (deprecated)"],
    responses={
        200: {"model": UpsertDeploymentResponse},
        201: {"model": UpsertDeploymentResponse},
    }
)
async def upsert_deployment(
    request: Request,
    project_name: str,
    deployment_data: UpsertDeploymentRequest = Body(...),
) -> JSONResponse:
    """Create or update deployment (sync, deprecated - use /api/v2 instead)."""

    # Create async task
    task = await create_async_task(
        task_type="upsert_deployment",
        project_name=project_name,
        deployment_name=deployment_data.deploymentName,
        payload={...},
    )

    # Wait for completion (blocking)
    try:
        completed_task = await wait_for_task_completion(
            task_id=task["task_id"],
            timeout_seconds=1800,  # 30 minutes
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Operation timed out after 30 minutes"
        )

    # Extract result from completed task
    if completed_task["status"] == "failed":
        raise HTTPException(
            status_code=400,
            detail=f"Operation failed: {completed_task.get('error_message')}"
        )

    result = completed_task.get("result", {})

    # Transform result to old response format
    return UpsertDeploymentResponse(
        status="success",
        message=f"Deployment '{deployment_data.deploymentName}' created/updated",
        deployment=...,  # from result
        urls=...,        # from result
        processing={"status": "completed"}
    )
```

**Pattern:** All V1 endpoints follow this:
1. Validate input
2. Create async task
3. **Wait for completion** (blocking call)
4. Extract result
5. Transform to old response format
6. Return as before (200/201)
7. Add `tags=["v1 (deprecated)"]` to mark as deprecated

### Phase 5: Update Main Router

**File: `opi/api/router.py`** (top of file)

Include both v1 and v2 routers:

```python
from opi.api.v2.router import router as v2_router

# ... existing code ...

# Include routers
api_router.include_router(api_router)  # existing endpoints (v1)
api_router.include_router(v2_router, prefix="/v2")  # new endpoints (v2)
```

### Phase 6: OpenAPI/Docs Configuration

**File: `opi/server.py`** (update create_app function)

Configure OpenAPI to clearly show v1 vs v2:

```python
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # Sort paths by version for clarity
    paths = openapi_schema.get("paths", {})
    sorted_paths = {}

    # V2 first, then v1
    for path in sorted(paths.keys(), key=lambda p: (not p.startswith("/api/v2"), p)):
        sorted_paths[path] = paths[path]

    openapi_schema["paths"] = sorted_paths

    # Add info about deprecation
    openapi_schema["info"]["x-api-info"] = {
        "v1_status": "deprecated - use /api/v2",
        "v2_status": "current - recommended",
        "migration_docs": "See features/api-v2-migration.md"
    }

    app.openapi_schema = openapi_schema
    return app.openapi_schema
```

## Endpoints to Implement (In Order)

Start with these, applying the pattern from Phase 3 & 4:

1. ✅ **upsert_deployment** (template example)
   - POST /api/v2/projects/{project_name}/:upsert-deployment
   - POST /api/projects/{project_name}/:upsert-deployment (refactor v1)

2. **create_project**
   - POST /api/v2/projects
   - POST /api/projects (refactor v1)

3. **delete_deployment**
   - DELETE /api/v2/projects/{project_name}/:delete-deployment
   - DELETE /api/projects/{project_name}/:delete-deployment (refactor v1)

4. **update_image**
   - PUT /api/v2/projects/{project_name}/{deployment_name}/:update-image
   - PUT /api/projects/{project_name}/{deployment_name}/:update-image (refactor v1)

5. **clone_database**
   - POST /api/v2/projects/{project_name}/{deployment_name}/:clone-database
   - POST /api/projects/{project_name}/{deployment_name}/:clone-database (refactor v1)

6. **clone_bucket**
   - POST /api/v2/projects/{project_name}/{deployment_name}/:clone-bucket
   - POST /api/projects/{project_name}/{deployment_name}/:clone-bucket (refactor v1)

7. **refresh_deployment**
   - POST /api/v2/projects/{project_name}/{deployment_name}/:refresh
   - POST /api/projects/{project_name}/{deployment_name}/:refresh (refactor v1)

## Testing Strategy

For each endpoint:

1. **V2 endpoint tests:**
   - Request returns 202 Accepted
   - Response has task_id, task_type, poll_url
   - Task can be polled and completes successfully
   - Task failures are captured correctly

2. **V1 endpoint tests:**
   - Request blocks until task completes
   - Returns same response format as before (200/201)
   - Timeout after 30 minutes
   - Errors are properly handled and returned

3. **Integration tests:**
   - Both V1 and V2 produce same result (async task completion)
   - Task worker processes both v1 and v2 tasks identically

## Migration Guide for API Clients

### For Current V1 Users

**Option 1: Stay on V1 (blocking)**
- No changes needed
- Endpoints continue working
- Will eventually be deprecated (timeline: TBD)

**Option 2: Migrate to V2 (async)**
- Change `/api/projects/...` → `/api/v2/projects/...`
- Change request handling:
  ```
  OLD: response = await client.post("/api/projects/...")
       use response immediately

  NEW: response = await client.post("/api/v2/projects/...")
       task_id = response.json()["task_id"]
       while True:
           status = await client.get(f"/api/tasks/{task_id}")
           if status["status"] in ["completed", "failed"]:
               use status["result"]
               break
           await asyncio.sleep(1)
  ```

### Documentation

Create `features/api-v2-migration.md` with:
- Quick start guide
- Code examples for polling
- Common patterns
- Error handling
- Timeline for v1 deprecation

## Success Criteria

- ✅ All long-running operations have both v1 (blocking) and v2 (async) versions
- ✅ V1 endpoints work transparently using async tasks internally
- ✅ V2 endpoints return 202 immediately and clients can poll
- ✅ `/docs` shows both with clear v1 (deprecated) vs v2 (current) tags
- ✅ Task system powers all operations
- ✅ No code duplication between v1 and v2 implementations
- ✅ Backward compatibility maintained for existing clients
- ✅ Clear migration path for clients to upgrade to v2

## Timeline

- **Phase 1-2:** Task helpers & models (foundation)
- **Phase 3:** V2 endpoints implementation (all 7 operations)
- **Phase 4:** V1 refactoring to use tasks (all 7 operations)
- **Phase 5:** Router integration
- **Phase 6:** Documentation and testing

## Notes

- Task timeout for blocking V1 calls: 30 minutes (configurable)
- All operations share same async task system
- Quick operations (list, get, validate) stay synchronous
- Task worker already handles all operation types
- No breaking changes to existing API surface