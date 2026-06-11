# Backup Retention Sweep

## What it is

A daily background job in the Operations Manager that deletes **orphaned**
backup snapshots — snapshots that no backup run will ever clean up again.

Kopia retention is applied by the backup pod at the end of each backup run,
scoped to that run's source identity. That means retention silently stops for:

- deployments that were deleted (e.g. PR previews),
- deployments whose `backup.schedule` was removed from the project file,
- projects with `backup.enabled: false`,
- legacy snapshots written under a broken Kopia source identity
  (`<uid>@<pod-name>` instead of the stable `opi-backup@...`), which no
  `kopia snapshot expire` ever matched.

Without the sweep, those snapshots live forever. With it, a source that stops
being backed up ages out to zero after a grace period.

## How it works

The sweep runs once per day from the backup scheduler loop, on the first tick
at or after 06:00 Europe/Amsterdam (after the default 02:00 backups and their
catch-up window). Per project on this cluster it lists all Kopia snapshots and
classifies each one (first matching rule wins):

| Rule | Verdict |
|---|---|
| `trigger:manual` tag, or source host ends in `-manual` | **Protected** — never touched. Manual backups are only removed explicitly by an operator. |
| Source identity or timestamp missing/unparseable | **Unclassifiable** — skipped with a warning. The sweep only deletes what it can positively classify. |
| Identity `opi-backup@...` and the deployment currently has a `backup.schedule` | **Active** — left to the backup pod's per-run retention. |
| Anything else, newer than the grace period | **Young orphan** — kept for now. |
| Anything else, older than the grace period | **Orphan** — deleted (or logged in dry-run). |

Note that the classification is identity-aware: a legacy `uid@podname`
snapshot is treated as an orphan even when its deployment tag points at an
actively scheduled deployment, because per-run retention can never match it.

Whole-project deletion is out of scope: deleting a project already marks its
entire backup prefix for deferred deletion.

> [!WARNING]
> Setting `backup.enabled: false` on a project that still exists makes the
> sweep treat **all** of that project's scheduled snapshots as orphans. They
> are protected only by the grace period: every snapshot older than
> `BACKUP_ORPHAN_RETENTION_DAYS` (default 30) is deleted on the first sweep
> after the flag is flipped, and the rest age out as they cross the boundary.
> If you intend to keep historical backups while pausing new ones, do not rely
> on this flag — the snapshots are not retained indefinitely. Keep the first
> production sweep in dry-run and review the manifest before arming deletion.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `BACKUP_SWEEP_ENABLED` | `true` | Master switch for the daily sweep. |
| `BACKUP_SWEEP_DRY_RUN` | `true` | Log a manifest of what would be deleted, delete nothing. |
| `BACKUP_ORPHAN_RETENTION_DAYS` | `30` | Grace period: orphans younger than this are kept. Mirrors `BACKUP_RETENTION_KEEP_DAILY`, so an orphaned source's backups survive as long as an active one's would. |

## Rollout

The sweep ships with `BACKUP_SWEEP_DRY_RUN=true`. Review at least one sweep
manifest in the OPI logs (`grep "Sweep "` / `grep "would delete"`), confirm
the candidates are right, then set `BACKUP_SWEEP_DRY_RUN=false`.

Example log output:

```
Backup retention sweep starting (cluster=odcn-production, grace=30d, dry_run=True)
Sweep wies/rig-prd-wies: 226 snapshots — {'active': 55, 'orphan-expired': 54, 'orphan-young': 117}
Sweep wies/rig-prd-wies: would delete orphan snapshot 3f1f... (1001730000@db-backup-production-postgresq-20260422-012810, deployment=production, ts=2026-04-22T01:28:10Z)
...
Backup retention sweep finished (would delete 54 snapshots)
```

## Dependencies

- Backup scheduler (`opi/core/backup_scheduler.py`) — hosts the daily trigger.
- Kopia CLI in the OPI image — the sweep queries and deletes directly, no
  pods are spawned.
- Namespace SOPS key — repository passwords are derived per namespace, so the
  sweep covers any namespace that still exists. Snapshots of fully deleted
  projects are handled by the project-deletion flow instead.
