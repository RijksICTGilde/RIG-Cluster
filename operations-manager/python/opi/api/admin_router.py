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
    effective_grace = grace_period_days if grace_period_days is not None else settings.DELETION_GRACE_PERIOD_DAYS

    service = _get_marked_for_deletion_service()

    # Get expired marks filtered by project
    all_expired = await service.get_expired_marks(effective_grace)
    project_expired = [m for m in all_expired if m["project_name"] == project_name]

    if not project_expired:
        return JSONResponse(
            content={
                "message": f"No expired marks found for project '{project_name}'",
                "project_name": project_name,
                "grace_period_days": effective_grace,
                "dry_run": dry_run,
                "purged": [],
                "errors": [],
            },
            status_code=200,
        )

    results: dict[str, Any] = {
        "project_name": project_name,
        "grace_period_days": effective_grace,
        "dry_run": dry_run,
        "purged": [],
        "errors": [],
    }

    # Import reconciliation purge helpers to reuse the same logic
    from opi.jobs.reconciliation import (
        _purge_backup_data,
        _purge_minio_bucket,
        _purge_minio_policy,
        _purge_minio_user,
        _purge_namespace,
        _purge_postgres_database,
        _purge_postgres_user,
    )

    pool = get_database_pool("main")

    # Group by type for ordered deletion
    db_marks = [m for m in project_expired if m["resource_type"] == "postgresql_database"]
    db_user_marks = [m for m in project_expired if m["resource_type"] == "postgresql_user"]
    bucket_marks = [m for m in project_expired if m["resource_type"] == "minio_bucket"]
    minio_user_marks = [m for m in project_expired if m["resource_type"] == "minio_user"]
    minio_policy_marks = [m for m in project_expired if m["resource_type"] == "minio_policy"]
    backup_marks = [m for m in project_expired if m["resource_type"] == "backup_data"]
    namespace_marks = [m for m in project_expired if m["resource_type"] == "namespace"]

    # Purge PostgreSQL resources
    if db_marks or db_user_marks:
        try:
            from opi.connectors.postgres import create_postgres_connector

            postgres_conn = create_postgres_connector(
                host=settings.DATABASE_HOST,
                admin_username=settings.DATABASE_ADMIN_NAME,
                admin_password=settings.DATABASE_ADMIN_PASSWORD,
            )
            for mark in db_marks:
                await _purge_postgres_database(postgres_conn, mark, service, results, dry_run)
            for mark in db_user_marks:
                await _purge_postgres_user(postgres_conn, mark, service, results, dry_run)
        except Exception as e:
            error_msg = f"Failed to initialize PostgreSQL connector for cleanup: {e}"
            logger.exception(error_msg)
            results["errors"].append(error_msg)

    # Purge MinIO resources (policies -> users -> buckets)
    if minio_policy_marks or minio_user_marks or bucket_marks:
        try:
            from opi.connectors.minio_mc import create_minio_connector
            from opi.core.cluster_config import get_minio_host, get_minio_port

            minio_conn = create_minio_connector()
            alias = "default-minio"
            minio_host = get_minio_host(None)
            minio_port = get_minio_port(None)
            minio_url = f"{'https' if settings.MINIO_USE_TLS else 'http'}://{minio_host}:{minio_port}"
            await minio_conn.configure_alias(
                alias, minio_url, settings.MINIO_ADMIN_ACCESS_KEY, settings.MINIO_ADMIN_SECRET_KEY
            )

            for mark in minio_policy_marks:
                await _purge_minio_policy(minio_conn, alias, mark, service, results, dry_run)
            for mark in minio_user_marks:
                await _purge_minio_user(minio_conn, alias, mark, service, results, dry_run)
            for mark in bucket_marks:
                await _purge_minio_bucket(minio_conn, alias, mark, service, results, dry_run)
        except Exception as e:
            error_msg = f"Failed to initialize MinIO connector for cleanup: {e}"
            logger.exception(error_msg)
            results["errors"].append(error_msg)

    # Purge backup data (Kopia snapshots)
    for mark in backup_marks:
        await _purge_backup_data(mark, service, results, dry_run)

    # Purge namespaces (only when all conditions met)
    for mark in namespace_marks:
        await _purge_namespace(mark, service, pool, results, dry_run)

    logger.info(
        "Cleanup for project '%s': purged=%d, errors=%d, dry_run=%s",
        project_name,
        len(results["purged"]),
        len(results["errors"]),
        dry_run,
    )

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
