"""De onderdelen van een backup-run opzoeken bij Kopia.

Het projectbestand zegt alleen WELKE run terug moet (``clone-from.run``); de onderdelen
zelf zijn runtime-gegevens en staan niet in het bestand. Daardoor is er een plek die
snapshots naar runs groepeert: de restore-wizard en de managers die data terugzetten
lezen dezelfde definitie van wat een onderdeel is.
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
    """De snapshots van een deployment, gegroepeerd per backup-run.

    Een snapshot zonder ``backup_run_id`` staat op zichzelf en vormt een run met zijn
    eigen snapshot-id als sleutel.
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
    """De onderdelen van precies deze backup-run.

    Raises:
        ValueError: als de run niet (meer) bij deze deployment bestaat. Een lege lijst
            teruggeven zou een leeg herstel als geslaagd laten eindigen.
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
    """De onderdelen van de backup-run die het ``clone-from``-blok van deze deployment noemt.

    De run hoort bij de bron-deployment (``reference``), dus cluster en namespace komen
    van die deployment en niet van de deployment die hersteld wordt.
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
