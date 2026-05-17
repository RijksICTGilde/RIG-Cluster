# Fix clone-status zombie databases

## Problem

When a project-processing run for a deployment with `clone-from` fails *after* the database clone succeeds but *before* `set_clone_status(completed=True)` is written, the next refresh fires the generational failover and creates a versioned database (`_v1`, `_v2`, ...). After 5 attempts the cap kicks in and the deployment is permanently blocked until a human drops the zombie databases.

This is a structural pattern, not a bug in the failover logic itself: the failover is doing exactly what it was designed to do, but the design assumes step 1's success implies the whole project run will reach step 6.

We have hit and "fixed" the symptom of this multiple times — each time the latest source of failure between steps 1 and 6 gets patched, but the structural vulnerability remains. New sources keep appearing.

## Concrete trace from the most recent occurrence

The cert for `keycloak.rijksapp.nl` expired on 2026-04-26. The morning after, the first deployment of `regel-k4c` PR589 ran:

```
2026-04-27 10:56:46.057  Successfully cloned database from regel_k4c_regelrecht to regel_k4c_pr589
2026-04-27 10:56:46.139  Deployment pr589 does not use MinIO service, skipping
2026-04-27 10:56:46.186  Creating Keycloak client for deployment pr589
2026-04-27 10:56:46.302  urllib3 ... SSLError(SSLCertVerificationError ... CERTIFICATE_VERIFY_FAILED)
2026-04-27 10:56:46.320  Error setting up SSO-Rijk integration for pr589
2026-04-27 10:56:46.324  Error processing project: Can't connect to server
2026-04-27 10:56:46.325  Project processing failed
                         <- set_clone_status(completed=True) never reached
```

State left behind:
- `regel_k4c_pr589` exists (a valid clone)
- `clone-from.status.completed = False` (never written, never committed to git)
- Every subsequent refresh saw `completed=False`, triggered failover, walked through `_v1`...`_v5`, hit the cap on 2026-05-01

Past incidents have followed the same shape with different trigger errors (ArgoCD permission denied, MinIO failures, git push races, etc.).

## Mechanism in the code

`opi/manager/database_manager.py:665`:
```python
if target_db_exists and (force_clone or generation is None):
    # Generational failover — create _vN
```

This is reached only when `_ensure_database_state` decides to clone. That decision is made at line 486:
```python
if clone_mode == "once" and clone_status_completed and not force_clone:
    # ... skipping clone
    clone_from = None
```

So failover is suppressed when `clone-from.status.completed=True`. The status flag is set in `opi/manager/project_manager.py:3871`, AFTER:
1. DB clone (`db_manager.create_resources_for_deployment`)
2. MinIO clone
3. Keycloak client
4. Redis
5. Manifest generation (`_process_application_manifests`)
6. **Then** `set_clone_status(completed=True)` is called for any deployment with clones performed
7. `commit_and_push` to git

Any exception in steps 2–5 (or push failure in 7) leaves the DB created but the status uncompleted. Next refresh → failover → zombie DB.

The status is set late on purpose (per the comment at `project_manager.py:3855-3857`):
> Generate application manifests (including PVC) BEFORE setting clone status. PVC clone relies on `clone-from.status.completed` being false to include `dataSource`.

So the same `completed` flag is overloaded: it gates the DB failover decision *and* it gates the PVC `dataSource` inclusion. A failure between the two stages of usage leaves the system inconsistent.

## Fix options

### Option A — Set status=completed right after each clone reports success

Move the `set_clone_status` call out of the end-of-run loop and into each manager's clone reporter (DB clone reports → set status; MinIO clone reports → set status; PVC clone reports → set status). Decouple from manifest generation by giving PVC its own `pvc-status` flag (or per-service flags under `clone-from.status.{database,minio,pvc}.completed`) instead of one shared bit.

- **Pros**: localized.
- **Cons**: changes the YAML schema; per-service flags need migration for existing project files.

### Option B — Two-state `in-progress` + `completed` flag (recommended)

Add `clone-from.status.in-progress=True` at the start of the clone, persisted via a `commit_and_push`. Set `completed=True` at the end as today. Change the failover condition to skip if `in-progress=True OR completed=True`.

A half-failed clone (e.g. Keycloak cert expired) leaves `in-progress=True, completed=False`. Next refresh sees `in-progress=True`, trusts the existing DB, runs the full flow again. If everything succeeds this time, `completed=True` flips. No zombies.

Worst case: a permanently broken project (e.g. a deleted source DB) gets retried forever into the same target. This is the same behaviour as a non-clone-from deployment with a permanent error, and it's manually fixable. Keep the existing 5-attempt version cap as a defense for the edge case where the in-progress write itself fails to persist.

- **Pros**: minimal schema change. Eliminates zombie accumulation at the source. Defense-in-depth with the cap.
- **Cons**: adds a git push at the start of every clone.

### Option C — Make the failover heuristic content-aware

Change the failover check from "status.completed=False" to something like "target DB exists AND target schema is empty/missing".

```python
if target_db_exists and (force_clone or not target_has_data):
    # failover only if the target is actually empty/broken
```

- **Pros**: works retroactively for projects that don't have updated YAMLs. Self-healing.
- **Cons**: requires a probe query (`SELECT 1 FROM information_schema.tables WHERE table_schema = $1 LIMIT 1`) per clone decision. Could be wrong for clones whose source has empty schemas.

## Recommended approach

**Option B + the existing version cap.** Together they:
- Eliminate zombie accumulation at the source (in-progress flag short-circuits the failover for genuine retries)
- Preserve the cap as a defense for the edge case where the in-progress write itself fails
- Don't depend on per-service schema migrations

## Files to touch (Option B sketch)

- `opi/handlers/project_file_handler.py:1843` — extend `set_clone_status` to accept `in_progress: bool | None = None` so callers can flip either bit independently.
- `opi/manager/database_manager.py:486` — change the skip-clone condition:
  ```python
  if clone_mode == "once" and (clone_status_completed or clone_status_in_progress) and not force_clone:
  ```
- `opi/manager/project_manager.py` (around `process_project` orchestration) — set `in_progress=True` and `commit_and_push` before invoking the per-manager `create_resources_for_deployment` calls. Keep the existing end-of-run `completed=True` behaviour.
- Add a unit test that simulates a Keycloak failure mid-flight and verifies the next refresh does **not** fork a new generation.

## Cleanup for existing zombies

For each affected deployment:
1. Identify which DB is canonical: read the K8s `<deployment>-database` Secret in the deployment's namespace and decode the `database` field.
2. If the K8s Secret is missing entirely, the deployment never finalized — all generations (base + zombies) are unused and safe to drop.
3. If canonical = base name and zombies are `_v1..._vN`: drop the zombies.
4. If canonical = `_vN` (a versioned DB is currently in use): the YAML must already record `generation=N`. Drop the base and lower versions only after confirming no active references.

For the 2026-05-01 incident specifically: pr589 had no `pr589-database` Secret in `rig-prd-regel-k4c` (the deployment had never finalized), so all 6 DBs (base + `_v1..._v5`) and the role were dropped. Subsequent refresh succeeded with Keycloak healthy and `set_clone_status(completed=True)` landed in YAML.

Remaining known zombies as of 2026-05-01 (still on `rig-db-rw`, untouched):
- `regel_k4c_pr592_v1`
- `amtbz_2m9_productie_v1` (production deployment — verify K8s Secret carefully before any drop)

## Diagnostics

`scripts/grafana_loki_logs.py` (added during this incident) queries Loki via the operations-manager Grafana integration. Useful invocations:

```bash
# Errors and warnings for a window
GRAFANA_TOKEN=$(kubectl -n rig-prd-operations get secret operations-manager-env-secrets \
  -o jsonpath='{.data.GRAFANA_TOKEN}' | base64 -d) \
uv run python scripts/grafana_loki_logs.py --from now-7d

# Trace a single task end-to-end (DEBUG level)
uv run python scripts/grafana_loki_logs.py \
  --from <start> --to <end> --limit 5000 --level "" --grep "task-<id-prefix>"
```

To find zombies cluster-wide:
```sql
SELECT datname FROM pg_database
WHERE datname ~ '_v[0-9]+$' AND datname NOT LIKE 'template%'
ORDER BY datname;
```

## Related issues

- #56 — Database clone retry loop creates zombie databases (root issue, body updated 2026-05-01 with the corrected analysis)
- #54 — Recurring nightly backup failures for wies project (unrelated, separate symptom of post-clone-step failures during scheduled backups)
- #55 — ArgoCD permission denied on post-creation status check (one of the historical "step 5" failures that would trigger this same zombie pattern)
