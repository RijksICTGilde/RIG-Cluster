"""The backups block on the project-details page, owned by the backupable services (RC-24).

Backups are not a service of their own: a project can back something up exactly when it
uses a service that declares a ``backup_label`` (persistent-storage, both PostgreSQL
services, minio-storage). So the block is owned jointly -- every such service mixes in
``BackupsPageMixin`` -- and the collector renders the section once and mounts the router
once, because all owners hand back the same template and the same router object.

The block covers ONE deployment (its schedule, its actions, its snapshot table), but the
snapshots are fetched for the WHOLE project in a single request: listing them opens a
Kopia repository over S3, and one request per deployment once OOM-killed the pod (see
``backups_fragment``). Only the first deployment's block therefore carries the loader;
it fans its results out to every block via ``hx-swap-oob``.

The route lives here because a service that owns a block owns the endpoint that fills it
-- otherwise half the block stays behind in the general router. Everything heavy
(managers, config, connectors) is imported inside the handler, so the catalog module
itself stays import-light, exactly as for the other service hooks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import requires_sso
from opi.services.catalog.base import DeploymentPageContext, DetailPageSection
from opi.services.catalog.events import on
from opi.services.services_enums import UIEvent

logger = logging.getLogger(__name__)

SECTION_TEMPLATE = "shared/section-backups.html.j2"

#: Mounted once by ``opi/web/router.py`` via ``registry.collect_service_routers()``,
#: however many services hand it back.
backups_router = APIRouter()


def _deployments_on_cluster(ctx: DeploymentPageContext) -> list[str]:
    """Names of the project's deployments on the managed cluster, sorted as displayed."""
    return sorted(
        deployment["name"]
        for deployment in ctx.project_data.get("deployments", []) or []
        if deployment.get("name") and deployment.get("cluster") == ctx.current_cluster
    )


def backup_deployment_sections(ctx: DeploymentPageContext) -> list[DetailPageSection]:
    """The backups block for ``ctx.deployment``, or nothing when it does not apply.

    Only deployments on the managed cluster have backups. When the backup service is
    unreachable the notice is shown once for the project, not once per deployment.
    """
    deployment_name = ctx.deployment.get("name")
    if not deployment_name or ctx.deployment.get("cluster") != ctx.current_cluster:
        return []

    on_cluster = _deployments_on_cluster(ctx)
    # The first block (in display order) carries the project's single snapshot loader
    # and any project-wide notice.
    is_first = bool(on_cluster) and on_cluster[0] == deployment_name

    if not ctx.backend_available.get("backups", False):
        if not is_first:
            return []
        return [DetailPageSection(template=SECTION_TEMPLATE, context={"available": False})]

    deployments = ctx.project_data.get("deployments", []) or []
    backup_config = ctx.deployment.get("backup")
    return [
        DetailPageSection(
            template=SECTION_TEMPLATE,
            context={
                "available": True,
                "project_name": ctx.project_data.get("name", ""),
                "deployment_name": deployment_name,
                "namespace": ctx.deployment.get("namespace", ""),
                "current_cluster": ctx.current_cluster,
                # The schedule modal is addressed by the deployment's index in the
                # project's own list (modal-edit-backup-schedule-<i>), not by name.
                "deployment_index": deployments.index(ctx.deployment) if ctx.deployment in deployments else 0,
                "schedule": (backup_config or {}).get("schedule", "") if isinstance(backup_config, dict) else "",
                "loads_snapshots": is_first,
            },
        )
    ]


class BackupsPageMixin:
    """Mixed into every service with a ``backup_label``: it brings the backups block.

    Each owner returns the same section and the same router, and the registry renders
    and mounts each of them once -- so the block appears for a project that can back
    something up, and exactly once.

    No cooperative ``super()`` needed since RC-39: a service can carry more than one page
    mixin -- the PostgreSQL services are backupable AND bring the console/job modals --
    and the event dispatch concatenates what every handler of the event returns, so
    neither mixin has to know the other exists. Before, a mixin that forgot to chain
    through ``super()`` silently swallowed the other's block.
    """

    @on(UIEvent.DEPLOYMENT_SECTIONS)
    def backups_block(self, ctx: DeploymentPageContext) -> list[DetailPageSection]:
        return backup_deployment_sections(ctx)

    def web_routers(self) -> list[Any]:
        return [*super().web_routers(), backups_router]  # type: ignore[misc]


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "pvc_name": snapshot.pvc_name,
        "timestamp": snapshot.timestamp,
        "size_bytes": snapshot.size_bytes,
        "cluster": snapshot.cluster,
        "namespace": snapshot.namespace,
        "project_name": snapshot.project_name,
        "deployment_name": snapshot.deployment_name,
        "component_name": snapshot.component_name,
        "storage_name": snapshot.storage_name,
        "generation": snapshot.generation,
        "backup_run_id": snapshot.backup_run_id,
        "resource_type": snapshot.resource_type,
        "tags": snapshot.tags,
        "trigger": snapshot.trigger,
    }


@backups_router.get("/projects/details/{project_name}/backups", response_class=HTMLResponse)
@requires_sso
async def backups_fragment(request: Request, project_name: str) -> HTMLResponse:
    """Backup snapshots for the WHOLE project, in one HTMX lazy-load.

    Listing snapshots opens a Kopia repository over S3 (2.1s connect + 0.4s list,
    measured in production), so it must stay off the detail page's own render. But
    it must be ONE request, not one per deployment: an earlier version gave every
    deployment block its own hx-trigger="load", and a project like 'wies' has 18
    deployments in a single namespace. That fired 18 parallel Kopia connects to the
    same repository, ~9 at once, and OOM-killed the pod.

    Deployments share a namespace, so this lists each namespace once (cached) and
    fans the results back out to the per-deployment blocks via hx-swap-oob.
    """
    from opi.core.auth_decorators import get_current_user
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.core.config import settings
    from opi.manager.backup import BackupManager
    from opi.services.project_authorization import is_user_authorized_for_project
    from opi.services.project_store import get_project_store
    from opi.web.lotc_switch import render

    user = get_current_user(request) or {}
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Not authorized")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    current_cluster = settings.CLUSTER_MANAGER
    deployments = [d for d in (project.data or {}).get("deployments", []) if d.get("cluster") == current_cluster]

    manager = BackupManager()
    # One list_snapshots per distinct namespace, not per deployment.
    per_namespace: dict[str, list[Any]] = {}
    error: str | None = None
    for deployment in deployments:
        base_namespace = deployment.get("namespace")
        if not base_namespace:
            continue
        k8s_namespace = get_prefixed_namespace(current_cluster, base_namespace)
        if k8s_namespace in per_namespace:
            continue
        try:
            per_namespace[k8s_namespace] = await manager.list_snapshots(
                current_cluster, k8s_namespace, project_name=project_name
            )
        except Exception as backup_err:
            # Shown, not swallowed: an empty list and an unreachable repository look
            # identical to a user otherwise.
            logger.warning(f"Failed to fetch backups for namespace {k8s_namespace}: {backup_err}")
            error = str(backup_err)
            per_namespace[k8s_namespace] = []

    backups_by_deployment: dict[str, list[dict[str, Any]]] = {}
    for deployment in deployments:
        base_namespace = deployment.get("namespace")
        name = deployment.get("name")
        if not base_namespace or not name:
            continue
        k8s_namespace = get_prefixed_namespace(current_cluster, base_namespace)
        backups_by_deployment[name] = [
            _snapshot_to_dict(s) for s in per_namespace.get(k8s_namespace, []) if s.deployment_name == name
        ]

    # Hetzelfde blok in twee vormgevingen. Het antwoord komt met hx-swap-oob binnen op een
    # pagina die of roos of NLDD is; kwam het altijd in roos-componenten terug, dan stond er
    # midden in een hertekende pagina een tabel in de oude vormgeving.
    return render(
        request,
        template="shared/_backup-snapshots-lotc.html.j2",
        context={
            "deployments": deployments,
            "backups_by_deployment": backups_by_deployment,
            "backups_error": error,
        },
    )
