"""Look up the items of a backup run at Kopia.

The project file only says WHICH run has to come back (``clone-from.run``); the items
themselves are runtime data and are not in the file. So the grouping of snapshots into
runs lives here, and the restore wizard and the managers that put data back read the
same definition of what an item is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.manager.backup.pvc_backup import BackupManager
from opi.manager.clone_validation import resolve_clone_source_deployment

if TYPE_CHECKING:
    from collections.abc import Iterable

    from opi.manager.backup.base import SnapshotInfo


def group_snapshots_into_runs(
    snapshots: Iterable[SnapshotInfo],
    deployment_name: str,
) -> dict[str, dict[str, Any]]:
    """The snapshots of a deployment, grouped per backup run.

    A snapshot without ``backup_run_id`` stands on its own and forms a run keyed by its
    own snapshot id.
    """
    runs: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        if snapshot.deployment_name != deployment_name:
            continue

        run_id = snapshot.backup_run_id or snapshot.snapshot_id
        run = runs.setdefault(
            run_id,
            {
                "backup_run_id": run_id,
                "timestamp": snapshot.timestamp,
                "deployment_name": deployment_name,
                "resource_count": 0,
                "resource_types": [],
                "items": [],
            },
        )
        resource_type = snapshot.resource_type or "pvc"
        run["resource_count"] += 1
        if resource_type not in run["resource_types"]:
            run["resource_types"].append(resource_type)
        run["items"].append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "resource_type": resource_type,
                "component_name": snapshot.component_name,
                "storage_name": snapshot.storage_name,
                "reference_name": snapshot.storage_name or snapshot.pvc_name,
                "generation": snapshot.generation,
            }
        )
    return runs


async def backup_run_items(
    project_name: str,
    cluster: str,
    namespace: str,
    deployment_name: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """The items of exactly this backup run.

    Raises:
        ValueError: when the run no longer exists for this deployment. Returning an empty
            list would let an empty restore end as a success.
    """
    snapshots = await BackupManager().list_snapshots(cluster, namespace, project_name=project_name)
    runs = group_snapshots_into_runs(snapshots, deployment_name)
    run = runs.get(run_id)
    if run is None:
        raise ValueError(
            f"backup-run '{run_id}' van deployment '{deployment_name}' bestaat niet (meer) "
            f"in project '{project_name}', dus er is niets terug te zetten"
        )
    return run["items"]


async def clone_backup_items(
    project_data: dict[str, Any],
    project_name: str,
    deployment: dict[str, Any],
    clone_from: dict[str, Any],
) -> list[dict[str, Any]]:
    """The items of the backup run named in this deployment's ``clone-from`` block.

    The run belongs to the source deployment (``reference``), so cluster and namespace come
    from that deployment and not from the deployment being restored.
    """
    deployment_name = str(deployment.get("name") or "")
    reference = str(clone_from.get("reference") or "")
    if not reference:
        raise ValueError(f"clone-from type 'backup' voor deployment '{deployment_name}' zonder reference")

    run_id = str(clone_from.get("run") or "")
    if not run_id:
        raise ValueError(f"clone-from type 'backup' voor deployment '{deployment_name}' zonder run")

    source = resolve_clone_source_deployment(project_data, reference, deployment_name)
    source_cluster = str(source.get("cluster") or settings.CLUSTER_MANAGER)
    source_namespace = get_prefixed_namespace(source_cluster, str(source.get("namespace") or project_name))
    return await backup_run_items(project_name, source_cluster, source_namespace, reference, run_id)
