"""The backups block on the project-details page, owned by the backupable services (RC-24).

Backups are not a service of their own: a project can back something up exactly when it
uses a service that declares a ``backup_label`` (persistent-storage, both PostgreSQL
services, minio-storage). So the block is owned jointly -- every such service mixes in
``BackupsPageMixin`` -- and the collector renders the section once and mounts the router
once, because all owners hand back the same template and the same router object.

The block covers ONE deployment (its schedule, its actions, its snapshot table), and since
RC-100 it is a TAB of its own (``/projects/<project>/backups/<deployment>``) instead of a
block on the Deployments tab. It is therefore no longer offered on
``UIEvent.DEPLOYMENT_SECTIONS`` -- which is the generic "every service delivers its blocks
per deployment" hook -- but collected by name through :func:`collect_backups_sections`.

That is a deliberate exception to the generic mechanism, and the alternative was
considered: let a service DECLARE that its block deserves a tab, and derive the tab bar
partly from the registry. Measured first, as the plan asked: exactly two services deliver
a deployment section, and the other one (metrics-scraper) shows the same graphs the
Metrics tab already shows for every project. With one candidate, a hook for "which
services want a tab" is machinery for a case that does not exist (YAGNI), so Backups is
named -- once here, once in the tab list, once in the tab template -- exactly as Metrics
is. The day a second block wants a tab, that is the moment to generalise.

The snapshots are fetched for the WHOLE project in a single request: listing them opens a
Kopia repository over S3, and one request per deployment once OOM-killed the pod (see
``backups_fragment``). The page now carries one deployment's block, so that block carries
the loader; it fans its results out via ``hx-swap-oob``, and the placeholders of the
deployments that are not on this page simply are not there.

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

logger = logging.getLogger(__name__)

SECTION_TEMPLATE = "shared/section-backups.html.j2"

#: Mounted once by ``opi/web/router.py`` via ``registry.collect_service_routers()``,
#: however many services hand it back.
backups_router = APIRouter()


def backup_deployment_sections(ctx: DeploymentPageContext) -> list[DetailPageSection]:
    """The backups block for ``ctx.deployment``, or nothing when it does not apply.

    Only deployments on the managed cluster have backups.
    """
    deployment_name = ctx.deployment.get("name")
    if not deployment_name or ctx.deployment.get("cluster") != ctx.current_cluster:
        return []

    if not ctx.backend_available.get("backups", False):
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
                # The page carries ONE deployment (RC-100), so this block carries the
                # loader. It used to be "only the first deployment of the project", when
                # every deployment had a block on the same page and eighteen loaders were
                # eighteen Kopia connects; on a page with one block that rule meant the
                # second deployment's page never loaded a thing.
                "loads_snapshots": True,
            },
        )
    ]


class BackupsPageMixin:
    """Mixed into every service with a ``backup_label``: it brings the backups tab.

    Carrying this mixin is what makes a service backupable in the UI: the tab shows its
    block for a project that uses at least one such service (see
    :func:`collect_backups_sections`), and every owner hands back the same router object
    so the fragment route is mounted once instead of once per owner.

    The block hung on ``UIEvent.DEPLOYMENT_SECTIONS`` until RC-100, and that is why the
    mixin no longer declares a handler: the block has a tab of its own now and is asked
    for by name. What it still does is say WHO owns backups, in one place.
    """

    def web_routers(self) -> list[Any]:
        return [*super().web_routers(), backups_router]  # type: ignore[misc]


def collect_backups_sections(ctx: DeploymentPageContext) -> list[DetailPageSection]:
    """The backups blocks for the Backups tab, or nothing for a project without backups.

    Same selection rule as the registry's own collectors -- the services the project
    actually uses -- but asked by name instead of through an event, because the block
    landed on a tab of its own (see the module docstring for the trade-off). A project
    that uses no backupable service gets no block, which is the failure this guards: a
    tab that shows "geen backups" for a project that cannot back anything up at all reads
    as "the backups are gone".
    """
    from opi.services.registry import selected_services

    if not any(isinstance(service, BackupsPageMixin) for service in selected_services(ctx.project_data)):
        return []
    return backup_deployment_sections(ctx)


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
        template="shared/_backup-snapshots.html.j2",
        context={
            "deployments": deployments,
            "backups_by_deployment": backups_by_deployment,
            "backups_error": error,
        },
    )
