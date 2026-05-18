# Tenant isolation — open follow-ups

PR #70 closed the two original VULNs (namespace pin via `get_deployments`,
wizard-create existence check). The review found three more issues; two are
addressed by this same PR with augmented commits, one remains open and is
documented here.

## Status overview

| Item                                                                | Status                              |
|---------------------------------------------------------------------|-------------------------------------|
| VULN 1: `get_deployments` namespace pin                             | ✅ in PR #70 (`enforce_namespace_pin`) |
| VULN 2: wizard-create existence check                               | ✅ in PR #70 (`simple_background`)  |
| VULN 2 bypass: git-monitor path                                     | ✅ in PR #70 (`git_monitor.check_and_create_namespaces`) |
| GAT 1: `task_handlers_project.handle_create_project` existence check | ✅ added in augmented commit         |
| GAT 3: `extract_deployment_namespace` pin enforcement (9 callsites) | ✅ added in augmented commit (helper-level fix) |
| GAT 2: TOCTOU on concurrent wizard-create                           | ⏳ open — design needed              |
| Refactor: reuse `git.check_overwrite_project_file`                  | ⏳ small follow-up                   |
| UX: ValueError → 400 instead of 500                                 | ⏳ small follow-up                   |

## GAT 2 — TOCTOU race on concurrent create

**The race.** `file_exists` reads the local working copy of the git
repository. `push_changes` (called by `create_or_update_file` when
`do_commit_and_push=True`) does up to `max_retries=3` with rebase on a
non-fast-forward push. Sequence under contention:

1. Tenant A submits create-project request for name `target`. OPI's
   `file_exists` returns `False` (no such file in local clone).
2. Tenant B submits create-project request for the same name `target` at
   roughly the same time. OPI's local clone still says no such file.
3. Both flows pass the existence-check guard.
4. Both flows attempt to commit and push. The first push succeeds. The
   second push is rejected as non-fast-forward, OPI rebases, **the rebase
   succeeds with no conflict because each create wrote a different content
   to the same file path**, and the second push goes through. Last-write
   wins, silent takeover.

**Requirement gate before fixing.** Concurrent wizard-creates with the same
target name require two SSO sessions to coordinate within seconds. Low
likelihood, real if intentional.

**Possible designs.**

1. **Re-check after rebase.** After `push_changes` reports a rebase
   happened, call `file_exists` again. If the file now exists (i.e. the
   other tenant's create won the race), abort with the same error message.
   Simplest fix; works because rebase exposes the conflicting file.
2. **Cluster-side advisory lock.** A `LockSecret` / `LockConfigMap` in the
   ops namespace, acquired with optimistic concurrency (`resourceVersion`)
   before `file_exists` and released after `push`. Robust, more moving
   parts.
3. **Switch from optimistic to per-project file-create-only semantics.** Use
   git's atomic ref operations (e.g. `git update-ref` with expected
   value) so a "create only if doesn't exist" semantic is enforced at the
   git layer, not the file-system layer. Cleanest but rewires the
   GitConnector.

Recommend (1) as the practical fix; (2)/(3) only if we see real contention.

## Refactor: reuse `git.check_overwrite_project_file`

`opi/connectors/git.py:1196` already implements an existence-check helper
(`check_overwrite_project_file`). PR #70 added an inline check in
`simple_background.process_project_background` and the augmented commit
adds another inline check in `task_handlers_project.handle_create_project`.
Both should ideally call the existing helper for consistency, instead of
duplicating the `file_exists` + error-message pattern. Pre-existing
duplication, low priority.

## UX: ValueError → 400 Bad Request

After GAT 3, `extract_deployment_namespace` raises `ValueError` when the
declared namespace mismatches the project name. The callers (backup_router,
restore_router, backup_tasks, router_detail_edit) do not catch this; FastAPI
turns an uncaught `ValueError` into a 500 Internal Server Error. The body
content is generic, so no information leak, but the user sees an opaque
error instead of "namespace must match project name".

Recommend a global exception handler that maps `ValueError` from these
paths to `HTTPException(status_code=400, detail=str(e))`. Better still: a
dedicated `TenantIsolationError` subclass so the handler is precise.

Pre-existing pattern in this codebase: most endpoints already wrap their
own `ValueError`s into `HTTPException`. The new raise-paths in
`extract_deployment_namespace` should match.

## Note on the documented error-message

`enforce_namespace_pin` (and the augmented `extract_deployment_namespace`)
raises with both `project_name` and `declared_namespace` in the message.
This is not an information leak: the attacker submitted both values
themselves. The message is useful for legitimate users who typo'd. Keep
as-is.
