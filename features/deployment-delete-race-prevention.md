# Deployment delete: race prevention and orphan detection

## What it is

Hardening for concurrent deployment mutations after the **toets-hn7/pr-36**
incident (2026-06-24): two sibling deployment deletes of the same project ran
concurrently, collided on the shared project file during `git push`, hit an
unrecoverable rebase conflict, and left the deployment half-deleted. The same
bug class produced the durable **toets-hn7/pr-32** orphan (a deployment removed
from the project file on 2026-06-12 whose ArgoCD Application and pods kept
running for 12 days, undetected).

Three changes address it, plus a remediation runbook for existing orphans.

## Background: why it happened

Every deployment of a project writes the **same** shared git files:
`projects/<project>.yaml` and the per-project ArgoCD `kustomization.yaml`. The
async task worker ran two deletes for the same project concurrently because the
in-flight guard keyed on `project_name` **and** `deployment_name`. A long
ArgoCD-prune wait (3.5 min for pr-36) widened the window for the sibling delete
to land on `main`, so pr-36's rebase could not auto-merge the overlapping list
edit. The push path aborted on conflict and failed the task terminally; only the
blind task auto-retry recovered it. When the retry does not recover (pr-32), the
orphan is permanent because nothing reconciles deployment-level drift.

## The changes

### 1a. Serialize project-file-mutating tasks per project

`AsyncTaskService.claim_next_task` no longer starts a pending task while another
**project-file-mutating** task for the same project is in flight, regardless of
deployment. The set is `PROJECT_FILE_MUTATING_TASK_TYPES` (upsert/update-image/
delete deployment, refresh, create-project, add-component(-to-deployment),
add-service). Clone/backup/restore are excluded so a slow restore never blocks
deploys. Non-mutating tasks keep the original per-deployment behavior.

This removes the pr-36/pr-37 collision at the source. Cross-project work still
runs in parallel; only same-project mutations serialize.

### 1b. Self-heal the project-file push on a true rebase conflict

`GitConnector.push_changes` / `commit_and_push` accept an optional `reapply`
async callback. On a rebase conflict (non-fast-forward then unmergeable):

- With `reapply`: hard-reset to the current remote, invoke `reapply` to
  re-apply the intended change on top of it, re-commit, and retry the push. This
  converges on a textual conflict that is semantically trivial.
- Without `reapply`: raise the new typed `GitPushConflictError`.

The deployment-delete path (step 8, removing the deployment from the project
file) passes a `reapply` that re-reads the project file fresh and re-removes the
deployment. This covers residual races that 1a cannot (notably the resource
auto-tuner, which commits to project files outside the task system).

### 2a. Deployment drift report (read-only)

`GET /api/v2/admin/deployments/drift` (admin API key) compares deployments
declared in project files against live ArgoCD Applications and reports:

- `orphaned_deployments`: live application with no project-file entry (the pr-32
  case),
- `missing_deployments`: declared but no live application.

It performs **zero** mutations (mirrors the service-orphan sweep's "no
auto-delete from a scan" rule). Logic lives in `opi/jobs/deployment_drift.py`
(`classify_deployment_drift`, pure and unit-tested); the endpoint lists
Applications via kubectl using the `project` label so it does not depend on
ArgoCD RBAC.

Project-infrastructure apps (`{project}-infrastructure`, which manage per-project
infra such as PostgreSQL clusters) carry the same `project` label but are not
deployments and never appear in `deployments[]`. They are explicitly excluded so
they are not mis-reported as orphans. Validated against live production (93
ArgoCD apps, 36 project files): the report returns exactly the one real orphan
(`toets-hn7-pr-32`), zero false positives, zero missing.

```bash
curl -X GET "https://<opi-host>/api/v2/admin/deployments/drift" \
  -H "X-API-Key: <admin-api-key>"
```

## Remediating an existing orphan (e.g. pr-32)

Drift is report-only; cleanup is deliberate. Prefer driving the delete through
OPI once this build is rolled out: re-add the deployment to its project file,
let OPI adopt it on reprocess, then `DELETE /api/v2/projects/<project>/<dep>`
for a full, idempotent teardown (services, namespace, manifests, app).

Manual Git fallback (two phases, matching OPI's own ordering to avoid a
finalizer deadlock):

1. In the ArgoCD-applications repo: delete
   `odcn-production/<project>/<project>-<dep>-argocd-application.yaml` and remove
   its line from `odcn-production/<project>/kustomization.yaml`. Commit + push.
   Wait for ArgoCD to prune the Application (its `resources-finalizer` removes
   the pods).
2. After the app is gone, in the deployments repo: delete the
   `odcn-production/<project>/<dep>/` manifest folder. Commit + push.

Also clear any now-stale `marked_for_deletion` row (a delete that timed out on
the ArgoCD wait marks `deployment_manifests` for deferred cleanup; a later retry
that deletes the manifests can leave the row behind):

```bash
curl -X GET  "https://<opi-host>/api/v2/admin/marked-for-deletion?project_name=<project>" -H "X-API-Key: <key>"
curl -X DELETE "https://<opi-host>/api/v2/admin/marked-for-deletion/<mark_id>"          -H "X-API-Key: <key>"
```

## Files

- `opi/core/async_task_service.py` - `PROJECT_FILE_MUTATING_TASK_TYPES`, per-project claim guard
- `opi/connectors/git.py` - `GitPushConflictError`, `reapply` hook, `_reset_to_remote`
- `opi/manager/delete_project_manager.py` - step 8 passes a `reapply` closure
- `opi/jobs/deployment_drift.py` - drift classification
- `opi/api/admin_router.py` - `GET /api/v2/admin/deployments/drift`

### Tests

- `tests/test_async_task_service.py` - claim-guard query wiring (mocked)
- `tests/test_async_task_claim_serialization_db.py` - real-Postgres proof of 1a
  (the pr-36/pr-37 scenario, backups not over-serialized, cross-project
  parallelism). Marked `requires_infra`; run with `TEST_DATABASE_DSN` set against
  an ephemeral Postgres (see the module docstring).
- `tests/test_git_push_conflict.py` - 1b control flow (mocked git)
- `tests/test_git_push_conflict_integration.py` - real-git end-to-end: forces an
  actual rebase conflict and proves the reapply path converges while preserving
  the concurrent writer's change.
- `tests/test_deployment_drift.py` - drift classification incl. infra-app exclusion

## Dependencies

None new. Uses the existing async task queue, GitConnector, kubectl connector,
and admin API-key auth.
