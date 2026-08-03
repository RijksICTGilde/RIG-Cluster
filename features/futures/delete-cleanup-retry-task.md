# Project delete: async, self-healing cleanup as a tracked task

> **Status: PLANNED / not started.** Design agreed (make the cleanup a visible,
> retryable task in the tasks table — not an invisible background loop).
> Implementation not begun.

## Problem

Project deletion deletes the ArgoCD Application and then **waits for ArgoCD to
confirm** the app (and its resources) are gone before removing the namespace. On a
busy cluster that confirmation regularly **times out**. When it does, the delete
leaves behind:

- a dangling ArgoCD `Application` stuck in `sync=Unknown` (source path gone), and
- (sometimes) an empty namespace whose removal was gated on that confirmation.

Today nothing retries this. The orphans accumulate across runs (we watched
`invit-*`/`pgsch-*` apps pile up as `Unknown` during an E2E run) and the only fix
is a human running kubectl by hand — exactly the "steeds handmatig mannetjes
erachteraan sturen" we want to stop. The E2E suite already carries a
`force_cleanup_project` helper (`tests/e2e/helpers/cluster.py`) that reclaims these
by hand — proof of both the failure mode and the exact steps a fix must automate.

There is a related parked idea (project delete should itself be an async task; see
`api-ui-test-parity-and-project-crud-api.md`). This plan is the natural home for it:
make delete async **and** make its cleanup self-heal.

## Decision

When cleanup can't be confirmed within the timeout, **do not** leave an orphan and
**do not** spawn an invisible loop. Instead **enqueue a tracked task in the tasks
table** that retries the cleanup until it succeeds. Visible, auditable, retryable —
an operator can see it pending/failing in the same place as every other task,
rather than wondering whether some hidden goroutine will ever get to it.

## Design

### 1. Delete becomes an async task (`DELETE_PROJECT`)

Give delete the same shape as create/configure: a typed async task processed by the
task worker. The HTTP/UI delete enqueues a `DELETE_PROJECT` task and returns
immediately with a task id the UI can poll — instead of blocking the request on
ArgoCD teardown. (Today create is async but delete is synchronous; this closes that
inconsistency.)

### 2. The confirmation timeout schedules a retry instead of orphaning

The delete task runs the normal teardown. If the ArgoCD-app-deletion confirmation
times out, the task **does not fail silently and does not mark success**. It records
the unconfirmed resources and **enqueues a `CLEANUP_PROJECT` task** (or re-enqueues
itself with a `cleanup-only` flag) carrying the project name + the specific
leftovers to reclaim. The originating delete task completes with a clear
"cleanup deferred to task <id>" outcome so the audit trail is unbroken.

### 3. `CLEANUP_PROJECT` task = idempotent reclaim, retried by the worker

The cleanup task does exactly what the manual/E2E cleanup does today, idempotently
(safe to run many times):

1. Delete any ArgoCD `Application` labelled `project=<name>` (`--ignore-not-found`).
2. If it hangs on the `resources-finalizer`, clear the finalizer
   (`remove_argocd_application_finalizers`, already exists) so pruning completes.
3. Delete the per-project `AppProject`.
4. Delete the project namespace(s) once no app is guarding them.
5. Remove any leftover git dirs (deployments / user-applications) — same as the
   orphan sweep done by hand earlier.

It reuses the existing `_cleanup_orphaned_argocd_resources` logic in
`delete_project_manager.py` rather than duplicating it. Success = every target
confirmed gone; otherwise it raises, and the **task worker's existing retry/backoff
+ stale-recovery** (`opi/core/task_worker.py`, `max_attempts`,
`TASK_WORKER_STALE_THRESHOLD`) re-runs it until it converges or exhausts attempts —
at which point it surfaces as a **failed task an operator can see and act on**,
which is the whole point.

### 4. A periodic reconcile enqueues cleanup for orphans nobody scheduled

Belt-and-suspenders: a lightweight periodic scan (the task worker already has
maintenance loops) lists ArgoCD apps whose project no longer exists and, for any it
finds, **enqueues a `CLEANUP_PROJECT` task**. This catches orphans from crashes or
pre-async deletes. Crucially it does not clean up *inline* — it only schedules
tracked tasks, so every reclaim is still visible in the tasks table.

## Why a task, not a background loop

- **Visible & auditable**: it shows up in the tasks table / UI like create/configure;
  an operator sees "cleanup pending/failed for project X" instead of guessing.
- **Reuses proven machinery**: retry, backoff, heartbeats, stale-recovery, progress
  already exist for tasks — no new bespoke scheduler.
- **Idempotent + safe to retry**: the reclaim steps are all `--ignore-not-found`.
- **Terminates loudly**: after max attempts it becomes a *failed task*, not a silent
  orphan — the failure is the signal.

## Open questions / next steps

1. Confirm task types: reuse one `DELETE_PROJECT` with a `phase=cleanup` re-enqueue,
   or a separate `CLEANUP_PROJECT` type. Separate type is clearer in the tasks list.
2. `max_attempts` / backoff tuning for cleanup vs. the create defaults (a namespace
   finalizer drain can legitimately take a while; don't exhaust attempts too fast).
3. Dedupe: don't enqueue a second cleanup task for a project that already has one
   pending (key on project name + a unique constraint / pre-check).
4. UI: surface the deferred-cleanup task id on the delete result so users can follow
   it; show orphan-cleanup tasks in the tasks view.
5. Make the E2E `force_cleanup_project` call the same code path (or assert the task
   was enqueued) so tests exercise the real mechanism instead of a parallel one.
6. Interaction with `pvc-readd-marked-for-deletion-conflict.md` and marked-for-delete
   namespaces — make sure cleanup respects/clears those marks.
