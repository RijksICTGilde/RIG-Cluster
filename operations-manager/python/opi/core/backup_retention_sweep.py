"""Daily retention sweep for orphaned backup snapshots.

Kopia retention is only applied by the backup pod at the end of a backup run,
scoped to that run's source identity. Snapshots that no longer receive backup
runs therefore never expire:

- deployments that were deleted (PR previews),
- deployments whose backup schedule was removed,
- legacy snapshots written under a broken source identity (uid@podname
  instead of the stable opi-backup@... identity), which no expire ever
  matched.

This sweep walks every project's backup repository once a day, classifies
each snapshot, and deletes orphaned non-manual snapshots once they are older
than ``BACKUP_ORPHAN_RETENTION_DAYS``. A source that stops being backed up
thus ages out to zero instead of living forever.

Classification (first matching rule wins):

1. ``trigger:manual`` tag or ``-manual`` source host -> protected, never
   touched. This is the long-standing promise that manual backups are only
   removed explicitly by an operator.
2. Source identity missing or timestamp unparseable -> unclassifiable,
   skipped with a warning. The sweep only deletes what it can positively
   classify.
3. Correct identity (user ``opi-backup``) belonging to a deployment that
   currently has a backup schedule -> active, left to the backup pod's own
   per-run retention.
4. Everything else is an orphan: deleted once older than the grace period,
   kept (logged) while younger.

With ``BACKUP_SWEEP_DRY_RUN`` enabled the sweep logs a manifest of what it
would delete without deleting anything.

Whole-project deletion is not handled here: deleting a project already marks
its entire backup prefix for deferred deletion.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings

if TYPE_CHECKING:
    from opi.manager.backup.base import SnapshotInfo

logger = logging.getLogger(__name__)

# The username every correctly-written snapshot carries (see
# kopia_backup_identity in opi/manager/backup/base.py).
_EXPECTED_SOURCE_USER = "opi-backup"

PROTECTED = "protected"
UNCLASSIFIABLE = "unclassifiable"
ACTIVE = "active"
ORPHAN_YOUNG = "orphan-young"
ORPHAN_EXPIRED = "orphan-expired"


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Parse a Kopia snapshot timestamp to an aware UTC datetime."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def classify_snapshot(
    snapshot: SnapshotInfo,
    active_deployments: set[str],
    cutoff: datetime,
) -> str:
    """Classify a snapshot for the retention sweep.

    Args:
        snapshot: The snapshot to classify.
        active_deployments: Deployment names that currently have a backup
            schedule (for the namespace this snapshot lives in).
        cutoff: Snapshots older than this moment are expired orphans.

    Returns:
        One of PROTECTED, UNCLASSIFIABLE, ACTIVE, ORPHAN_YOUNG,
        ORPHAN_EXPIRED.
    """
    # Manual backups are never touched, regardless of identity. The trigger
    # tag also covers legacy manual snapshots written under a broken source
    # identity.
    if snapshot.trigger == "manual":
        return PROTECTED
    if snapshot.source_host and snapshot.source_host.endswith("-manual"):
        return PROTECTED

    # Only delete what we can positively classify.
    if not snapshot.source_user or not snapshot.source_host:
        return UNCLASSIFIABLE

    # Correct identity + scheduled deployment: the backup pod applies
    # retention on every run; nothing for the sweep to do.
    if snapshot.source_user == _EXPECTED_SOURCE_USER and snapshot.deployment_name in active_deployments:
        return ACTIVE

    timestamp = _parse_timestamp(snapshot.timestamp)
    if timestamp is None:
        return UNCLASSIFIABLE

    return ORPHAN_EXPIRED if timestamp < cutoff else ORPHAN_YOUNG


class BackupRetentionSweep:
    """Sweeps orphaned backup snapshots across all projects on this cluster."""

    def __init__(self, cluster: str):
        self._cluster = cluster

    async def run(self) -> None:
        """Run one sweep over all projects' backup repositories."""
        from opi.services.project_service import get_project_service

        dry_run = settings.BACKUP_SWEEP_DRY_RUN
        cutoff = datetime.now(UTC) - timedelta(days=settings.BACKUP_ORPHAN_RETENTION_DAYS)
        logger.info(
            "Backup retention sweep starting (cluster=%s, grace=%dd, dry_run=%s)",
            self._cluster,
            settings.BACKUP_ORPHAN_RETENTION_DAYS,
            dry_run,
        )

        projects = get_project_service().get_all_projects()
        total_deleted = 0
        for project in projects.values():
            if not project.data:
                continue
            project_name = project.data.get("name", "")
            if not project_name:
                continue
            try:
                total_deleted += await self._sweep_project(project_name, project.data, cutoff, dry_run)
            except Exception:
                logger.exception("Backup retention sweep failed for project %s — continuing", project_name)

        logger.info(
            "Backup retention sweep finished (%s %d snapshots)",
            "would delete" if dry_run else "deleted",
            total_deleted,
        )

    def _namespaces_and_active_deployments(self, project_data: dict) -> dict[str, set[str]]:
        """Map each k8s namespace of this project to its actively scheduled deployments.

        Every namespace with at least one deployment on this cluster is
        included (so namespaces whose deployments all lost their schedule are
        still swept). A deployment counts as active when it has a
        backup.schedule and project-level backups are not disabled.
        """
        project_backup = project_data.get("backup", {})
        backups_enabled = not isinstance(project_backup, dict) or project_backup.get("enabled", True)

        namespaces: dict[str, set[str]] = {}
        for deployment in project_data.get("deployments", []):
            deployment_name = deployment.get("name", "")
            raw_namespace = deployment.get("namespace", "")
            if not deployment_name or not raw_namespace:
                continue
            if deployment.get("cluster", "") != self._cluster:
                continue

            namespace = get_prefixed_namespace(self._cluster, raw_namespace)
            active = namespaces.setdefault(namespace, set())

            dep_backup = deployment.get("backup", {})
            has_schedule = isinstance(dep_backup, dict) and dep_backup.get("schedule")
            if backups_enabled and has_schedule:
                active.add(deployment_name)

        return namespaces

    async def _sweep_project(
        self,
        project_name: str,
        project_data: dict,
        cutoff: datetime,
        dry_run: bool,
    ) -> int:
        """Sweep all namespaces of one project; returns number of (would-be) deletions."""
        from opi.manager.backup import create_backup_manager

        deleted = 0
        for namespace, active_deployments in self._namespaces_and_active_deployments(project_data).items():
            backup_manager = create_backup_manager()
            snapshots = await backup_manager.list_snapshots(
                cluster=self._cluster, namespace=namespace, project_name=project_name
            )
            if not snapshots:
                continue

            expired: list[SnapshotInfo] = []
            counts: dict[str, int] = {}
            for snapshot in snapshots:
                verdict = classify_snapshot(snapshot, active_deployments, cutoff)
                counts[verdict] = counts.get(verdict, 0) + 1
                if verdict == UNCLASSIFIABLE:
                    logger.warning(
                        "Sweep %s/%s: cannot classify snapshot %s (source=%s@%s, ts=%r) — skipping",
                        project_name,
                        namespace,
                        snapshot.snapshot_id,
                        snapshot.source_user,
                        snapshot.source_host,
                        snapshot.timestamp,
                    )
                elif verdict == ORPHAN_EXPIRED:
                    expired.append(snapshot)

            logger.info("Sweep %s/%s: %d snapshots — %s", project_name, namespace, len(snapshots), counts)

            for snapshot in expired:
                logger.info(
                    "Sweep %s/%s: %s orphan snapshot %s (%s@%s, deployment=%s, ts=%s)",
                    project_name,
                    namespace,
                    "would delete" if dry_run else "deleting",
                    snapshot.snapshot_id,
                    snapshot.source_user,
                    snapshot.source_host,
                    snapshot.deployment_name,
                    snapshot.timestamp,
                )

            if dry_run:
                deleted += len(expired)
                continue

            results = await backup_manager.delete_snapshots(
                cluster=self._cluster,
                namespace=namespace,
                snapshot_ids=[s.snapshot_id for s in expired],
                project_name=project_name,
            )
            failed = [snapshot_id for snapshot_id, ok in results.items() if not ok]
            if failed:
                logger.warning(
                    "Sweep %s/%s: failed to delete %d snapshots: %s", project_name, namespace, len(failed), failed
                )
            deleted += sum(1 for ok in results.values() if ok)

        return deleted
