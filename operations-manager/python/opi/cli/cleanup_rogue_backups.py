"""Find and (optionally) delete duplicate scheduled backup runs.

Background: a scheduler bug caused some deployments to back up multiple times
per day during May 16-17 2026 instead of once. Kopia retention will eventually
prune duplicates that share a calendar day in the same source, but if multiple
backup runs each landed in their own day-bucket (e.g. across UTC midnight) or
if retention couldn't run for some reason, surplus snapshots may still exist.

This CLI walks every (project, deployment) that has a scheduled backup, groups
its SCHEDULED snapshots by Europe/Amsterdam calendar day, and for every day
with more than one run keeps the **latest** and marks the rest as rogue.

Safety properties:
- Manual snapshots (``trigger == "manual"``) are NEVER touched.
- Snapshots without a deployment_name tag are NEVER touched (can't safely attribute them).
- Each day always retains at least one scheduled run.
- Dry-run by default. ``--confirm`` is required to actually delete.

Run inside the OPI pod where it has all credentials, no port-forward/API key needed::

    kubectl exec -n rig-prd-operations \\
      $(kubectl get pod -n rig-prd-operations \\
        -l app.kubernetes.io/name=operations-manager -o name) \\
      -- python -m opi.cli.cleanup_rogue_backups

    # Restrict to one deployment first if you want to verify:
    kubectl exec ... -- python -m opi.cli.cleanup_rogue_backups --deployment wies/production

    # Actually delete (after reviewing dry-run output):
    kubectl exec ... -- python -m opi.cli.cleanup_rogue_backups --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import logging
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from opi.connectors.kopia import KopiaConnector, KopiaRepositoryConfig, create_kopia_connector
from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.core.rrule_utils import parse_rrule
from opi.manager.backup import create_backup_manager
from opi.manager.backup.base import SnapshotInfo  # noqa: TC001

logger = logging.getLogger(__name__)

_AMS = ZoneInfo("Europe/Amsterdam")
_UTC = ZoneInfo("UTC")


async def _derive_backup_password(kubectl, namespace: str) -> str:
    """Reproduce BaseBackupManager._derive_backup_key() outside of a manager.

    Falls back to a namespace-based key when no SOPS secret is found, exactly
    like the manager does — but in that case we won't be able to read the
    repo anyway, since the password won't match. The fallback exists so the
    code doesn't crash, but the caller will see a Kopia connection failure
    and skip the deployment.
    """
    age_key = await kubectl.get_sops_secret_from_namespace(namespace)
    if not age_key:
        age_key = f"fallback-key-{namespace}"
    material = f"kopia-backup-{namespace}-{age_key}".encode()
    derived = hashlib.sha256(material).digest()
    return base64.b64encode(derived).decode()[:32]


async def _build_kopia_config(kubectl, project_name: str, namespace: str, cluster: str) -> KopiaRepositoryConfig:
    backup_config = create_backup_manager().config
    return KopiaRepositoryConfig(
        s3_endpoint=backup_config.s3_endpoint,
        s3_bucket=backup_config.get_bucket_name(project_name, cluster),
        s3_access_key=backup_config.s3_access_key,
        s3_secret_key=backup_config.s3_secret_key,
        s3_prefix=f"{cluster}/{namespace}",
        password=await _derive_backup_password(kubectl, namespace),
        use_tls=backup_config.s3_use_tls,
    )


def _parse_amsterdam_local(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_AMS)


def _group_scheduled_runs_by_day(
    snapshots: list[SnapshotInfo],
    deployment_name: str,
    target_hour: int,
    target_minute: int,
) -> tuple[list[tuple[datetime, str, list[SnapshotInfo]]], list[tuple[datetime, str, list[SnapshotInfo]]]]:
    """Group SCHEDULED snapshots for this deployment by Amsterdam day, by run.

    Returns (rogue_runs, keeper_runs). Each run tuple is
    (earliest_ts_local, backup_run_id, [snapshots]).

    The "keeper" for each day is the run with start time **closest** to that
    day's BYHOUR:BYMINUTE target — preserving the snapshot that best matches
    the user's intended schedule. Ties broken by earliest.

    Safety: snapshots are filtered by deployment_name AND trigger==scheduled
    AND must have a backup_run_id (we don't delete things we can't group cleanly).
    """
    eligible = [
        s for s in snapshots if s.deployment_name == deployment_name and s.trigger == "scheduled" and s.backup_run_id
    ]
    if not eligible:
        return [], []

    by_run: dict[str, list[SnapshotInfo]] = defaultdict(list)
    for s in eligible:
        by_run[s.backup_run_id or ""].append(s)

    runs: list[tuple[datetime, str, list[SnapshotInfo]]] = []
    for rid, items in by_run.items():
        # Use the earliest snapshot timestamp in this run as the run's start time.
        ts_candidates = [_parse_amsterdam_local(s.timestamp) for s in items]
        ts_local_list = [t for t in ts_candidates if t is not None]
        if not ts_local_list:
            continue
        runs.append((min(ts_local_list), rid, items))

    by_day: dict[str, list[tuple[datetime, str, list[SnapshotInfo]]]] = defaultdict(list)
    for run in runs:
        day_key = run[0].date().isoformat()
        by_day[day_key].append(run)

    target_minutes = target_hour * 60 + target_minute

    def _distance_to_target(run_tuple: tuple[datetime, str, list[SnapshotInfo]]) -> tuple[int, datetime]:
        local_dt = run_tuple[0]
        run_minutes = local_dt.hour * 60 + local_dt.minute
        return (abs(run_minutes - target_minutes), local_dt)

    rogue: list[tuple[datetime, str, list[SnapshotInfo]]] = []
    keepers: list[tuple[datetime, str, list[SnapshotInfo]]] = []
    for day_runs in by_day.values():
        day_runs.sort(key=_distance_to_target)
        keepers.append(day_runs[0])  # closest to BYHOUR:BYMINUTE
        rogue.extend(day_runs[1:])
    return rogue, keepers


async def _delete_snapshot_safely(
    kopia: KopiaConnector,
    kopia_config: KopiaRepositoryConfig,
    snapshot: SnapshotInfo,
    expected_project: str,
    expected_deployment: str,
) -> tuple[bool, str]:
    """Verify the snapshot's tags match before deleting. Returns (ok, message)."""
    fresh = await kopia.get_snapshot(kopia_config, snapshot.snapshot_id)
    if not fresh:
        return False, "not found"
    if fresh.project_name and fresh.project_name != expected_project:
        return False, f"project tag {fresh.project_name!r} != {expected_project!r}"
    if fresh.deployment_name and fresh.deployment_name != expected_deployment:
        return False, f"deployment tag {fresh.deployment_name!r} != {expected_deployment!r}"
    if fresh.trigger != "scheduled":
        return False, f"trigger {fresh.trigger!r} not 'scheduled'"
    success = await kopia.delete_snapshot(kopia_config, snapshot.snapshot_id)
    return (True, "deleted") if success else (False, "delete returned False")


def _iter_scheduled_deployments(cluster: str):
    """Yield (project_name, deployment_name, namespace, target_hour, target_minute)
    for every deployment on this cluster that has a backup schedule and isn't
    backup-disabled at the project level.
    """
    from opi.services.project_service import get_project_service

    for project in get_project_service().get_all_projects().values():
        if not project.data:
            continue
        project_name = project.data.get("name", "")
        if not project_name:
            continue
        project_backup = project.data.get("backup") or {}
        if isinstance(project_backup, dict) and not project_backup.get("enabled", True):
            continue
        for deployment in project.data.get("deployments") or []:
            deployment_name = deployment.get("name", "")
            if not deployment_name:
                continue
            if deployment.get("cluster", "") != cluster:
                continue
            dep_backup = deployment.get("backup") or {}
            if not isinstance(dep_backup, dict) or not dep_backup.get("schedule"):
                continue
            raw_namespace = deployment.get("namespace", "")
            if not raw_namespace:
                continue
            namespace = get_prefixed_namespace(cluster, raw_namespace)
            rrule = parse_rrule(str(dep_backup.get("schedule")))
            byhour_raw = rrule.get("BYHOUR", "2")
            byminute_raw = rrule.get("BYMINUTE", "0")
            target_hour = int(byhour_raw) if str(byhour_raw).isdigit() else 2
            target_minute = int(byminute_raw) if str(byminute_raw).isdigit() else 0
            yield project_name, deployment_name, namespace, target_hour, target_minute


class _KopiaErrorCapture(logging.Handler):
    """Capture WARNING+ messages from Kopia/PVC loggers during a list call.

    Kopia's connector returns ``[]`` on both empty-repo and connection-failure,
    so we can't tell them apart from the return value. Capturing the logger
    output lets us distinguish "no snapshots in a healthy repo" (no captured
    messages) from "couldn't reach Kopia" (one or more captured messages).
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _hook_kopia_errors() -> tuple[_KopiaErrorCapture, list[logging.Logger]]:
    handler = _KopiaErrorCapture()
    loggers = [
        logging.getLogger("opi.connectors.kopia"),
        logging.getLogger("opi.manager.backup.pvc_backup"),
        logging.getLogger("opi.manager.backup.database_backup"),
        logging.getLogger("opi.manager.backup.bucket_backup"),
    ]
    for lg in loggers:
        lg.addHandler(handler)
    return handler, loggers


def _unhook_kopia_errors(handler: _KopiaErrorCapture, loggers: list[logging.Logger]) -> None:
    for lg in loggers:
        lg.removeHandler(handler)


async def _run() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--deployment",
        help="Restrict to one project/deployment (format: project/deployment).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete rogue snapshots. Default is dry-run.",
    )
    args = parser.parse_args()

    target: tuple[str, str] | None = None
    if args.deployment:
        try:
            target = tuple(args.deployment.split("/", 1))  # type: ignore[assignment]
        except ValueError:
            print(f"ERROR: --deployment must be project/deployment, got {args.deployment!r}", file=sys.stderr)
            return 1
        if not target or len(target) != 2:
            print(f"ERROR: --deployment must be project/deployment, got {args.deployment!r}", file=sys.stderr)
            return 1

    cluster = settings.CLUSTER_MANAGER

    # ProjectService is an in-memory registry populated at OPI startup. In this
    # standalone CLI process the registry is empty, so we have to refresh from
    # git first (the same call the long-running server makes on a stale cache).
    print("Loading project files from git…", flush=True)
    from opi.core.startup import refresh_projects_from_git

    try:
        loaded = await refresh_projects_from_git()
    except Exception as e:
        print(f"ERROR: failed to load projects from git: {e}", file=sys.stderr)
        return 1
    print(f"Loaded {loaded} project(s).\n", flush=True)

    backup_manager = create_backup_manager()
    kopia = create_kopia_connector()
    kubectl = create_kubectl_connector()

    pairs = list(_iter_scheduled_deployments(cluster))
    if target:
        pairs = [t for t in pairs if (t[0], t[1]) == target]
        if not pairs:
            print(f"ERROR: no scheduled deployment matches {args.deployment!r}", file=sys.stderr)
            return 1

    mode = "DELETE" if args.confirm else "DRY-RUN"
    print(f"=== Backup cleanup {mode} (cluster={cluster}, {len(pairs)} deployment(s)) ===\n")

    grand_clean = 0
    grand_rogue_runs = 0
    grand_rogue_snapshots = 0
    grand_deleted = 0
    grand_failed = 0
    grand_skipped = 0

    for project_name, deployment_name, namespace, target_hour, target_minute in pairs:
        label = f"{project_name}/{deployment_name}"
        handler, hooked_loggers = _hook_kopia_errors()
        try:
            snapshots = await backup_manager.list_snapshots(
                cluster=cluster, namespace=namespace, project_name=project_name
            )
        except Exception as e:
            _unhook_kopia_errors(handler, hooked_loggers)
            print(f"{label}: ERROR listing snapshots: {e}")
            grand_skipped += 1
            continue
        else:
            _unhook_kopia_errors(handler, hooked_loggers)

        if handler.messages:
            # Kopia/listing failed silently (returns [] on connect/list errors).
            # Treat as unreachable rather than "clean" — we must not delete
            # anything when we don't have a reliable inventory.
            first = handler.messages[0]
            print(f"{label}: SKIPPED (Kopia unreachable: {first[:200]})")
            grand_skipped += 1
            continue

        rogue, keepers = _group_scheduled_runs_by_day(snapshots, deployment_name, target_hour, target_minute)
        if not rogue:
            if keepers:
                print(f"{label}: clean ({len(keepers)} day(s) with scheduled runs)")
            else:
                print(f"{label}: no scheduled runs found")
            grand_clean += 1
            continue

        rogue_snap_count = sum(len(items) for _, _, items in rogue)
        grand_rogue_runs += len(rogue)
        grand_rogue_snapshots += rogue_snap_count
        print(f"\n{label}: {len(rogue)} rogue run(s) / {rogue_snap_count} snapshot(s); {len(keepers)} keeper(s)")

        kopia_config: KopiaRepositoryConfig | None = None
        if args.confirm:
            try:
                kopia_config = await _build_kopia_config(kubectl, project_name, namespace, cluster)
            except Exception as e:
                print(f"  ERROR building Kopia config: {e} — skipping this deployment")
                grand_skipped += 1
                continue

        for local_dt, rid, items in sorted(rogue, key=lambda x: x[0]):
            print(f"  ROGUE  {local_dt.strftime('%Y-%m-%d %H:%M %Z')}  run={rid}  ({len(items)} snapshot(s))")
            if args.confirm and kopia_config is not None:
                for snap in items:
                    ok, msg = await _delete_snapshot_safely(kopia, kopia_config, snap, project_name, deployment_name)
                    icon = "✓" if ok else "✗"
                    print(f"    {icon} {snap.snapshot_id}: {msg}")
                    if ok:
                        grand_deleted += 1
                    else:
                        grand_failed += 1

        for local_dt, rid, _items in sorted(keepers, key=lambda x: x[0]):
            print(f"  KEEP   {local_dt.strftime('%Y-%m-%d %H:%M %Z')}  run={rid}")

    print("\n=== Summary ===")
    print(f"  Deployments checked:  {len(pairs)}")
    print(f"  Already clean:        {grand_clean}")
    print(f"  Skipped (errors):     {grand_skipped}")
    print(f"  Rogue runs found:     {grand_rogue_runs} ({grand_rogue_snapshots} snapshots)")
    if args.confirm:
        print(f"  Snapshots deleted:    {grand_deleted}")
        print(f"  Snapshots failed:     {grand_failed}")
    else:
        print("  Mode: DRY-RUN — re-run with --confirm to actually delete.")
    return 0 if grand_failed == 0 else 2


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
