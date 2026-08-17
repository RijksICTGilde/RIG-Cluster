# YAML/Git-diff-driven lifecycle reconciliation (design & vision)

Status: **design / vision** — not implemented. This document records the direction,
the evidence for why it does not work today, the core challenge, and a proposed
approach. It is deliberately design-level; no code here.

## 1. The vision

ZAD is YAML-driven. The project YAML in the `zad-projects` git repository is the
**single source of truth**. A git commit that **adds, changes, or removes** a project
YAML should drive **create / update / delete** of *all* of that project's resources:
namespaces, ArgoCD `Application`s, PostgreSQL databases/schemas/users, MinIO
buckets/users/policies, Keycloak realms/clients, secrets, and the generated K8s
manifests in `zad-deployments`.

The desired invariant: **running the YAML always reconciles actual cluster state to
it — including deletion.** If a project file disappears from git, its cluster
footprint should disappear too. Today that invariant holds for create/update but
**not** for delete.

## 2. Current state (with evidence)

### 2.1 Git monitoring does not process the full lifecycle
- `opi/core/git_monitor.py` → `file_change_handler(file_path, content)` reacts only to
  a *changed* file and merely ensures namespaces (`check_and_create_namespaces`).
  There is **no branch for a removed project file**, and no full processing
  (DB/bucket/argo/manifests) — that is all it does.
- `start_git_monitoring` passes only a single change-callback; nothing observes
  deletions.
- Defaults make it a demo, not the real processor: `ENABLE_GIT_MONITOR = False`, and
  `GIT_PROJECTS_SERVER_FILE_PATH = "projects/simple-example.yaml"` — it watches **one
  file**, not a directory of projects. Verified on the running sandbox: its configmap
  sets `ENABLE_GIT_MONITOR=false`.

### 2.2 Full processing and deletion live on the API/UI path
- Full project processing happens via the wizard/API (`process_project_background` →
  `process_project`), not from a git diff.
- Deletion happens **only** via the explicit API/UI path
  (`opi/manager/delete_project_manager.py`). That path waits for the ArgoCD
  `Application` object to disappear (`wait_for_application_deletion`, ~20 × 3 s) and
  returns **HTTP 207 (partial)** when the app is not confirmed gone in time — the
  "flaky delete" symptom is this wait timing out.

### 2.3 The reconcile GC deliberately does not auto-detect orphans
`opi/jobs/reconciliation.py` is a **mark-driven** garbage collector. It has purge
helpers for the backing resources — `_purge_namespace`, `_purge_postgres_database`,
`_purge_postgres_user`, `_purge_minio_bucket`, `_purge_minio_user`,
`_purge_minio_policy`, `_purge_keycloak_client`, `_purge_pvc`, `_purge_backup_data` —
but **step 3 is intentionally not automated**. Quoting the code:

> Orphan detection is deliberately NOT automated here [...] Auto-marking from a scan is
> forbidden: a wrong expected set would schedule live resources for deletion (see the
> waggl-9et near-miss).

So the safe cleanup flow is meant to be **report-first + human-confirmed**: the comment
references `orphans/report` / `orphans/confirm` admin endpoints, after which the job
purges confirmed candidates past a grace period.

> **Open question / possible gap:** those `orphans/report` / `orphans/confirm` endpoints
> could not be located in the codebase during this investigation. Either they are named
> differently, live elsewhere, or are not yet implemented. Confirm before relying on
> them.

### 2.4 The observed consequence
On the live sandbox the project store returns **0 projects** (`get_all() == []`) while
the cluster holds **50+ leftover `rig-*` namespaces** and **8 orphaned ArgoCD apps**
(paths like `./sandboxed-local/e2e43-po6/productie` with `ComparisonError: app path
does not exist`), plus 6 duplicate (identical, harmless) repo-credential secrets.
Projects were removed from git and **nothing was cleaned up**. This is the vision's
failure mode in the wild, and it degrades the sandbox enough to make subsequent
explicit deletes time out (the 207).

## 3. The core challenge

Robust, safe detection across git diffs of **what is new, what is changed, and what is
gone**, at the project (and deployment/component) level — and then dispatching each to
the right manager. Building blocks that already exist:
- Component-level diffing: `analyze_project_changes` in `opi/connectors/git.py`
  (file-scoped diff that detects removed components).
- Create/update: `process_project`. Delete: `delete_project_manager`.
- Mark/purge machinery + grace periods in `reconciliation.py`.

The hard part is **deletion safety**. A wrong or empty expected set must never
mass-delete live resources — that is exactly the waggl-9et near-miss that led to
disabling auto-orphan-purge. Any automation of "gone in git ⇒ delete in cluster" has to
carry strong guardrails or it becomes a foot-gun.

## 4. Proposed approach (options & trade-offs)

1. **Track the last successfully-processed commit** of the projects repo. Each poll,
   compute the git diff `previous → current`, classify every project file as
   `added` / `modified` / `removed`, and dispatch:
   - added/modified → `process_project` (idempotent; a manual YAML edit is authoritative)
   - removed → the delete path (`delete_project_manager`)
   This makes a plain `git commit` the API for the whole lifecycle.

2. **Guardrails on the destructive path** (non-negotiable given waggl-9et):
   - **Sanity thresholds**: refuse a diff that would delete more than *N* projects or
     more than *X%* of the known set in one run — fail closed, alert, require override.
   - **Report / dry-run mode**: surface the intended deletions before acting.
   - **Mark + grace period**: reuse the existing mark/purge machinery so a deletion is
     staged, not immediate, and a `git revert` within the grace window cancels it
     (the "unmark on git-revert recovery" path already exists in `reconcile()`).
   - **Confirmation for large destructive diffs**: human-in-the-loop only when the
     change is big; small, obviously-intentional single-project removals can flow with
     a grace period.

3. **Idempotent whole-set reconcile** each run so actual state converges to the YAML
   set, while deletion stays guarded as above.

4. **Make the ArgoCD-deletion wait deterministic.** Git-driven deletion hits the same
   `wait_for_application_deletion` timeout as the API path; this must be robust
   (adaptive wait / finalizer handling / clear terminal states) or git-delete inherits
   the 207 flakiness.

## 5. Open questions / risks to resolve before building

- Do the `orphans/report` / `orphans/confirm` endpoints exist, or must they be built?
  (§2.3 — could not be found.)
- How to make the ArgoCD `Application` deletion wait deterministic rather than a fixed
  60 s timeout.
- How to bound a "catastrophic diff" safely (threshold values; what counts as an
  override; alerting).
- Single-file vs directory-of-projects monitoring: the current monitor watches one
  file; full lifecycle needs the whole `zad-projects` tree.
- Concurrency: the API/UI path is a live writer of the same repo/resources. Git-diff
  reconciliation and interactive edits must not race or double-process.
- Recovery semantics: `git revert` should cleanly undo a pending deletion (unmark), and
  re-adding a project file should recreate it cleanly.

## 6. Relationship to the "flaky delete" investigation

The orphan accumulation in §2.4 is the root of what was earlier miscalled "flaky"
deletes: the sandbox is full of debris precisely because git-removal cleans up nothing,
and the explicit delete path then fights that debris (ArgoCD wait → 207). Fixing the
vision (safe git-diff-driven delete) and hardening the ArgoCD-deletion wait address the
same underlying problem from two directions.
