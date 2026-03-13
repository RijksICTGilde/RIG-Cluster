"""Admin API router for cleanup, reconciliation, and maintenance operations.

Provides endpoints for managing resource lifecycle operations that are
separate from the normal project API. Authenticated via ADMIN_API_KEY.

Endpoints:
    GET  /api/v2/admin/marked-for-deletion          - List marked resources
    POST /api/v2/admin/cleanup/trigger               - Trigger cleanup (purge expired)
    POST /api/v2/admin/reconciliation/trigger         - Trigger full reconciliation
    DELETE /api/v2/admin/marked-for-deletion/{mark_id} - Remove a specific mark
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_admin_api_key
from opi.core.config import settings
from opi.core.database_pools import get_database_pool
from opi.services.marked_for_deletion_service import MarkedForDeletionService

logger = logging.getLogger(__name__)

admin_router: APIRouter = APIRouter(
    prefix="/api/v2/admin",
    tags=["admin"],
    responses={
        401: {"description": "Unauthorized - invalid or missing ADMIN_API_KEY"},
        501: {"description": "ADMIN_API_KEY not configured"},
        500: {"description": "Internal server error"},
    },
    default_response_class=JSONResponse,
)


def _get_marked_for_deletion_service() -> MarkedForDeletionService:
    """Get a MarkedForDeletionService instance using the main database pool."""
    pool = get_database_pool("main")
    return MarkedForDeletionService(pool)


@admin_router.get("/marked-for-deletion")
@validate_admin_api_key
async def list_marked_for_deletion(
    request: Request,
    project_name: str | None = Query(None, description="Filter by project name"),
) -> JSONResponse:
    """List resources marked for deletion.

    Returns all resources currently in the marked_for_deletion table.
    Optionally filter by project_name to see marks for a specific project
    (works even if the project no longer exists).

    Example:
        curl -X GET "http://localhost:9595/api/v2/admin/marked-for-deletion?project_name=my-project" \\
          -H "X-API-Key: your-admin-api-key"
    """
    service = _get_marked_for_deletion_service()

    if project_name:
        marks = await service.get_marks_for_project(project_name)
    else:
        marks = await service.get_all_marks()

    return JSONResponse(
        content={
            "marks": marks,
            "total": len(marks),
            "filter": {"project_name": project_name},
        },
        status_code=200,
    )


@admin_router.post("/cleanup/trigger")
@validate_admin_api_key
async def trigger_cleanup(
    request: Request,
    project_name: str = Query(..., description="Project name to clean up (required)"),
    dry_run: bool = Query(True, description="Preview actions without executing (default: true)"),
    grace_period_days: int | None = Query(None, description="Override grace period in days"),
) -> JSONResponse:
    """Trigger cleanup of expired marked resources for a specific project.

    Purges resources that are marked for deletion AND past the grace period.
    Uses project_name from the marked_for_deletion table, so this works even
    if the project no longer exists in the system.

    IMPORTANT: dry_run defaults to true. Set dry_run=false to actually purge resources.

    Example:
        curl -X POST "http://localhost:9595/api/v2/admin/cleanup/trigger?project_name=my-project&dry_run=false" \\
          -H "X-API-Key: your-admin-api-key"
    """
    from opi.jobs.reconciliation import cleanup_project

    pool = get_database_pool("main")
    results = await cleanup_project(
        pool=pool,
        project_name=project_name,
        grace_period_days=grace_period_days,
        dry_run=dry_run,
    )

    if not results["purged"] and not results["errors"]:
        results["message"] = f"No expired marks found for project '{project_name}'"

    return JSONResponse(content=results, status_code=200)


@admin_router.post("/reconciliation/trigger")
@validate_admin_api_key
async def trigger_reconciliation(
    request: Request,
    dry_run: bool = Query(True, description="Preview actions without executing (default: true)"),
    grace_period_days: int | None = Query(None, description="Override grace period in days"),
) -> JSONResponse:
    """Trigger a full reconciliation run.

    Reconciliation performs three operations:
    1. Unmarks resources that reappeared in project YAMLs (git revert recovery).
    2. Purges resources that are marked AND past the grace period.
    3. (Future) Detects newly orphaned resources.

    Uses all currently loaded project YAML definitions as the source of truth.

    IMPORTANT: dry_run defaults to true. Set dry_run=false to actually purge resources.

    Example:
        curl -X POST "http://localhost:9595/api/v2/admin/reconciliation/trigger?dry_run=false" \\
          -H "X-API-Key: your-admin-api-key"
    """
    from opi.jobs.reconciliation import reconcile
    from opi.services.project_service import get_project_service

    pool = get_database_pool("main")
    project_service = get_project_service()

    # Build project YAML list from all loaded projects
    all_projects = project_service.get_all_projects()
    project_yamls: list[dict[str, Any]] = [p.data for p in all_projects.values() if p.data]

    results = await reconcile(
        pool=pool,
        project_yamls=project_yamls,
        grace_period_days=grace_period_days,
        dry_run=dry_run,
    )

    return JSONResponse(
        content={
            "message": "Reconciliation completed",
            "projects_evaluated": len(project_yamls),
            "dry_run": dry_run,
            "grace_period_days": grace_period_days or settings.DELETION_GRACE_PERIOD_DAYS,
            **results,
        },
        status_code=200,
    )


@admin_router.delete("/marked-for-deletion/{mark_id}")
@validate_admin_api_key
async def delete_mark(
    request: Request,
    mark_id: str,
) -> JSONResponse:
    """Remove a specific deletion mark without purging the resource.

    Use this to manually cancel the scheduled deletion of a resource.
    The resource itself is NOT deleted - only the mark is removed.

    Example:
        curl -X DELETE "http://localhost:9595/api/v2/admin/marked-for-deletion/some-uuid" \\
          -H "X-API-Key: your-admin-api-key"
    """
    service = _get_marked_for_deletion_service()

    deleted = await service.delete_mark(mark_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Mark '{mark_id}' not found",
        )

    return JSONResponse(
        content={"message": f"Mark '{mark_id}' removed successfully"},
        status_code=200,
    )
