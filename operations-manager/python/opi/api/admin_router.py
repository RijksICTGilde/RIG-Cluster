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
from opi.services.project_store import get_project_store

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

    pool = get_database_pool("main")

    # Build project YAML list from all loaded projects
    all_projects = get_project_store().get_all()
    project_yamls: list[dict[str, Any]] = [p.data for p in all_projects if p.data]

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


@admin_router.get("/orphans/report")
@validate_admin_api_key
async def orphan_sweep_report(request: Request) -> JSONResponse:
    """Run the read-only service-orphan sweep and return the report.

    Inventories PostgreSQL databases, Keycloak realms/clients and MinIO
    buckets, classified against the live project files. Performs ZERO
    mutations. Deletion requires POST /orphans/confirm with an explicit
    item list, followed by the normal grace-period purge.

    Example:
        curl -X GET "http://localhost:9595/api/v2/admin/orphans/report" \\
          -H "X-API-Key: your-admin-api-key"
    """
    from opi.jobs.service_orphan_sweep import sweep

    pool = get_database_pool("main")
    all_projects = get_project_store().get_all()
    project_yamls: list[dict[str, Any]] = [p.data for p in all_projects if p.data]

    report = await sweep(pool, project_yamls, cluster=settings.CLUSTER_MANAGER)
    return JSONResponse(content=report, status_code=200)


@admin_router.post("/orphans/confirm")
@validate_admin_api_key
async def confirm_orphans(request: Request) -> JSONResponse:
    """Mark confirmed orphan candidates for grace-period deletion.

    Body: {"items": [{"type": "...", "name": "...", "realm": "..."}]}
    with type one of postgresql_database, postgresql_user, minio_bucket,
    keycloak_client (keycloak_client requires "realm").

    Safety: the sweep is re-run server-side and each submitted item must
    still be classified ``orphan_candidate`` in the fresh report. Items
    that are expected, system, in_use_anomaly or unknown are rejected.
    Accepted items are marked in marked_for_deletion; actual deletion
    happens via the normal reconciliation purge after the grace period.

    Example:
        curl -X POST "http://localhost:9595/api/v2/admin/orphans/confirm" \\
          -H "X-API-Key: your-admin-api-key" -H "Content-Type: application/json" \\
          -d '{"items": [{"type": "postgresql_database", "name": "regel_k4c_pr104"}]}'
    """
    from opi.jobs.service_orphan_sweep import CONFIRMABLE, sweep

    body = await request.json()
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="Body must contain a non-empty 'items' list")

    pool = get_database_pool("main")
    all_projects = get_project_store().get_all()
    project_yamls: list[dict[str, Any]] = [p.data for p in all_projects if p.data]
    cluster = settings.CLUSTER_MANAGER

    report = await sweep(pool, project_yamls, cluster=cluster)

    # Index the fresh report by (type, name[, realm]) -> classification
    candidates: dict[tuple, dict[str, Any]] = {}
    for entry in report["databases"]:
        candidates[("postgresql_database", entry["name"])] = entry
        candidates[("postgresql_user", entry["name"])] = entry
    for entry in report["minio_buckets"]:
        candidates[("minio_bucket", entry["name"])] = entry
    for entry in report["keycloak_clients"]:
        candidates[("keycloak_client", entry["client_id"], entry["realm"])] = entry

    service = _get_marked_for_deletion_service()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for item in items:
        itype = item.get("type", "")
        name = item.get("name", "")
        realm = item.get("realm", "")
        key = ("keycloak_client", name, realm) if itype == "keycloak_client" else (itype, name)

        entry = candidates.get(key)
        if entry is None:
            rejected.append({**item, "reason": "not present in the current sweep report"})
            continue
        if entry["classification"] != CONFIRMABLE:
            rejected.append(
                {**item, "reason": f"classified '{entry['classification']}' - only orphan_candidate is confirmable"}
            )
            continue

        metadata: dict[str, Any] = {"confirmed_via": "orphans/confirm", "sweep_reason": entry["reason"]}
        if itype == "keycloak_client":
            metadata["realm"] = realm
        await service.mark_resource(
            resource_type=itype,
            resource_name=name,
            project_name=item.get("project_name", ""),
            deployment_name=item.get("deployment_name", ""),
            cluster=cluster,
            metadata=metadata,
        )
        accepted.append(item)

    return JSONResponse(
        content={
            "message": f"{len(accepted)} item(s) marked for deletion, {len(rejected)} rejected",
            "grace_period_days": settings.DELETION_GRACE_PERIOD_DAYS,
            "accepted": accepted,
            "rejected": rejected,
        },
        status_code=200,
    )
