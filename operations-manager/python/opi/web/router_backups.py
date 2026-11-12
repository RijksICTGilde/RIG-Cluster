"""The lazy-loaded snapshot table of the backups block.

The route lives here and the answer in the backupable services
(``opi/services/catalog/shared/backups.py``): an entry point translates and renders, the
service knows which snapshots there are.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.authz.actions import Action, action
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.core.errors import current_reference, with_reference
from opi.core.request_user import get_current_user
from opi.http.auth.auth_decorators import requires_sso
from opi.http.auth.authz import may_on_any_deployment
from opi.http.render.lotc_switch import render
from opi.manager.backup import BackupManager
from opi.services.catalog.shared.backups import _snapshot_to_dict
from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.project_store import get_project_store

logger = logging.getLogger(__name__)

backups_router = APIRouter()


@backups_router.get("/projects/details/{project_name}/backups", response_class=HTMLResponse)
@action(Action.BACKUP_READ)
@requires_sso
async def backups_fragment(request: Request, project_name: str) -> HTMLResponse:
    """Backup snapshots for the WHOLE project, in one HTMX lazy-load.

    Deployments share a namespace, so the gatherer lists each namespace once and this fans
    the results back out to the per-deployment blocks via hx-swap-oob.
    """
    user = get_current_user(request) or {}
    if not is_user_authorized_for_project(project_name, user.get("email", "").lower()):
        raise HTTPException(status_code=403, detail="Not authorized")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    deployments, backups_by_deployment, error = await _snapshots_by_deployment(project_name, project.data or {})

    # The same block in two designs. The answer arrives with hx-swap-oob on a page that is
    # either roos or NLDD; always returning roos components would put a table in the old design
    # in the middle of a redrawn page.
    return render(
        request,
        template="shared/_backup-snapshots.html.j2",
        context={
            "deployments": deployments,
            "backups_by_deployment": backups_by_deployment,
            "backups_error": error,
            # The restore button follows the role guard of the restore flow. On at least one
            # deployment, like the wizard itself: the user picks the target there.
            "mag_terugzetten": may_on_any_deployment(request, action=Action.RESTORE_RUN, project_name=project_name),
        },
    )


async def _snapshots_by_deployment(
    project_name: str, project_data: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], str | None]:
    """The deployments on this cluster and their snapshots, plus the error that broke a read.

    Listing snapshots opens a Kopia repository over S3 and takes seconds, so this stays off
    the detail page's own render (the block lazy-loads it). And it must be ONE request, not
    one per deployment: a project with eighteen deployments in one namespace fires eighteen
    parallel Kopia connects to the same repository, which OOM-kills the pod. Deployments
    share a namespace, so each namespace is listed once.
    """
    current_cluster = settings.CLUSTER_MANAGER
    deployments = [d for d in project_data.get("deployments", []) if d.get("cluster") == current_cluster]

    manager = BackupManager()
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
        except Exception:  # the contract of this read path: any failure is an empty result
            # Shown, not swallowed: an empty list and an unreachable repository look
            # identical to a user otherwise. The exception text stays in the log: a Kopia
            # or S3 failure names an endpoint, and this message lands on the screen.
            logger.exception("Failed to fetch backups for namespace %s", k8s_namespace)
            error = with_reference("De backups konden niet worden opgehaald.", current_reference())
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
    return deployments, backups_by_deployment, error
