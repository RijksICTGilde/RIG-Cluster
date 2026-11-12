"""The backups block on the project-details page, owned by the backupable services.

Backups are not a service of their own: a project can back something up exactly when it
uses a service that declares a ``backup_label`` (persistent-storage, both PostgreSQL
services, minio-storage, seaweedfs-storage). So the block is owned jointly -- every such
service mixes in ``BackupsPageMixin`` -- and the collector renders the section once and
mounts the router once, because all owners hand back the same template and the same router
object.

The block covers ONE deployment (its schedule, its actions, its snapshot table) and is a TAB
of its own (``/projects/<project>/backups/<deployment>``). It is therefore NOT offered on
``UIEvent.DEPLOYMENT_SECTIONS`` -- the generic "every service delivers its blocks per
deployment" hook -- but collected by name through :func:`collect_backups_sections`, the way
Metrics is. A hook for "which services want a tab" would be machinery for one case.

The snapshots are fetched for the WHOLE project in a single request: listing them opens a
Kopia repository over S3, and one request per deployment can OOM the pod (see
the gatherer below). The page carries one deployment's block, so that block carries the
loader; it fans its results out via ``hx-swap-oob``, and the placeholders of the deployments
that are not on this page simply are not there.

The route that serves the lazy load is in the entry layer (``opi/web/router_backups.py``):
it reads the snapshots through the backup manager and renders them with the shape this
module gives them.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.catalog.base import DeploymentPageContext, DetailPageSection

logger = logging.getLogger(__name__)

SECTION_TEMPLATE = "shared/section-backups.html.j2"


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
                # The page carries ONE deployment, so this block carries the loader.
                "loads_snapshots": True,
            },
        )
    ]


class BackupsPageMixin:
    """Mixed into every service with a ``backup_label``: it brings the backups tab.

    Carrying this mixin is what makes a service backupable in the UI: the tab shows its
    block for a project that uses at least one such service (see
    :func:`collect_backups_sections`). The route that fills the block is in the entry layer
    (``opi/web/router_backups.py``), mounted once.

    The mixin declares no event handler: the block has a tab of its own and is asked for by
    name. What it does say is WHO owns backups, in one place.
    """


def collect_backups_sections(ctx: DeploymentPageContext) -> list[DetailPageSection]:
    """The backups blocks for the Backups tab, or nothing for a project without backups.

    Same selection rule as the registry's own collectors -- the services the project
    actually uses -- but asked by name instead of through an event, because the block sits
    on a tab of its own. A project that uses no backupable service gets no block, which is
    the failure this guards: a tab that shows "geen backups" for a project that cannot back
    anything up at all reads as "the backups are gone".
    """
    from opi.services.registry import selected_services

    if not any(isinstance(service, BackupsPageMixin) for service in selected_services(ctx.project_data)):
        return []
    return backup_deployment_sections(ctx)


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:  # noqa: ANN401  # a kopia snapshot object
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
