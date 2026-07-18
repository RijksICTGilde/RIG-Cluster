# ProjectStore: fast, safe, backend-agnostic project-file access

> Status: design / futures. Not yet built. This document is the build brief: an engineer
> (or another Claude) should be able to implement Phase 1 from it without further context.

## Context and problem

Project definitions live as YAML in a single git repo (`zad-projects`, path `projects/{name}.yaml`).
Git is deliberately the source of truth: it is the only resource shared across clusters (each cluster
runs its own OPI with its own Postgres), it gives an audit history, and it stays human-editable.

Today the project-file access path is slow and has concurrency gaps. Ground truth from the code:

- **Reads are already fast.** `opi/services/project_service.py` is an in-memory singleton holding
  every parsed project (`_projects: dict[str, Project]`, each `Project.data` is the full parsed YAML).
  `get_project` / `get_all_projects` / `get_by_api_key` never touch git.
- **Every mutation clones the whole repo, twice.** A fresh `ProjectManager` (and its git connector)
  is built per HTTP request. `get_contents()` clones `zad-projects` (blobless, full history) to read
  authoritative state, then `save_and_commit_project()` clones again to write. Clones go to a fresh
  `tempfile.mkdtemp` under `/tmp` and are `rmtree`'d after. So a single edit = about two full-history
  blobless clones + one push, and the clone cost grows with the whole repo's history.
- **The 30s refresh re-clones everything.** `ensure_projects_fresh()` (30s TTL) does one shared
  blobless clone and re-reads every file, largely redundant because writes are already write-through.
- **Freshness is loose.** 30s TTL for web reads; pure API reads (`resource_router`) can be
  indefinitely stale; `git_monitor` (default off) does not feed the cache; no push/webhook invalidation.
- **No write lock.** Concurrency rests entirely on git push fetch+rebase+retry. Most edit paths call
  `save_and_commit_project` directly (only delete uses the re-read/re-apply `mutate_and_commit_project`),
  and a git rebase merges *text* without re-validating the merged *semantics*, so two edits to the same
  file have a lost-update / invalid-merge window.

Key file/line anchors (verify before editing, code moves):
- `opi/services/project_service.py` (in-memory cache, `register`, `load_project_from_data`, `replace_all_projects`)
- `opi/manager/project_manager.py`: `get_contents` (~6322), `save_and_commit_project` (~1434),
  `mutate_and_commit_project` (~1490), `_validate_structural_integrity` (~1327),
  `get_git_connector_for_project_files` (~641), per-deployment/argo connectors (~1248, ~1302)
- `opi/connectors/git.py`: per-request clone `ensure_repo_cloned` (~567), `__init__` tempdir (~127),
  `close`/rmtree (~1544), `push_changes` fetch+rebase+retry (~1390), `_rebase_on_remote` (~809),
  `reset_to_remote` (~861), `create_git_connector_for_project_files` (~1810, defaults `full_history=True`)
- `opi/core/startup.py`: `ensure_projects_fresh` (~291), `refresh_projects_from_git` (~318), `_setup_projects` (~616)

## Scope (and what stays out)

Three repos, two owners. **ProjectStore owns only `zad-projects`.**

| Repo | Contents | Owner |
|---|---|---|
| `zad-projects` | project files (`projects/{name}.yaml`) | **ProjectStore** (new) |
| `zad-deployments` | generated K8s manifests | ProjectManager git connectors, unchanged |
| `zad-argo-user-applications` | ArgoCD Application manifests | ProjectManager git connectors, unchanged |

Argo/deployment git performs fine and is explicitly **out of scope**. Do not touch
`get_git_connector_for_argocd()`, the per-deployment connectors, or manifest generate/commit/push.
The same warm-copy pattern could be applied there later, but not now.

**Assumption: OPI is single-replica per cluster today.** The in-process `asyncio.Lock` below relies on
this. Multi-replica is a stated future; see "Concurrency" for the swap (Postgres advisory lock or leader).

## The interface (backend-agnostic)

One abstraction all callers use. Nothing git-specific leaks (an opaque `ref: str`, not a commit SHA),
so a `DatabaseProjectStore` could replace `GitProjectStore` with no caller changes. Locking is NOT on
the interface: `mutate` is the atomic unit; a multi-step read-decide-write is expressed as one `change`
function inside one `mutate` call.

```python
class ProjectStore(ABC):
    """Single authority for reading and writing project files."""

    # reads: served from the in-memory cache, no I/O
    def get(self, name: str) -> Project | None: ...
    def get_all(self) -> list[Project]: ...                 # admin overview, instant
    def get_by_api_key(self, api_key: str) -> Project | None: ...
    def exists(self, name: str) -> bool: ...
    def get_decrypted(self, name: str) -> dict | None: ...  # view/edit; derived, never written back

    # mutations: serialized read-modify-write, validated before persist, ACID
    async def create(self, name: str, data: dict, *, message: str, actor: str) -> Project: ...
    async def mutate(self, name: str,
                     change: Callable[[dict], Awaitable[dict]],
                     *, message: str, actor: str) -> MutationResult: ...   # carries before + after + ref
    async def delete(self, name: str, *, message: str, actor: str) -> None: ...

    # audit / history: git log/show now, a versions table later
    async def history(self, name: str) -> list[Revision]: ...
    async def read_at(self, name: str, ref: str) -> dict: ...     # a specific past version
    async def previous(self, name: str) -> dict | None: ...       # last version before HEAD (see contract)

    # freshness / lifecycle
    async def reconcile(self) -> None: ...   # pull external edits into the cache (webhook/poll)
    async def bootstrap(self) -> None: ...   # startup: load all into cache
```

`Project`, `Revision`, `MutationResult` are plain backend-neutral dataclasses. `MutationResult` carries
`before`, `after`, and `ref` so downstream reconcile logic gets the diff handed to it (see Reconciliation).

**Correctness contract for `previous()`:** it must resolve the last commit that touched *that specific
path* (`git log -1 --skip=1 -- projects/{name}.yaml`), NOT `HEAD~1` (the parent may touch another project).

## GitProjectStore implementation

State (process-wide singleton for `zad-projects`):
- in-memory parsed cache (absorbs today's `ProjectService`),
- one **warm, long-lived working copy** on disk (not a per-request tempdir), cloned once with full
  history (blobless is fine; history is then local and cheap),
- a single `asyncio.Lock` (single-replica),
- a reused git connector (never `rmtree`'d).

Reads: from the in-memory cache. Instant.

`mutate` (the heart, single-replica):
```python
async with self._lock:                    # serialize same-repo writers in-process
    for attempt in range(MAX_RETRIES):
        current = self._cache[name].data    # latest committed (write-through keeps it current)
        new = await change(current)          # apply the change on the freshest state
        validate(new)                        # schema + structural, fails closed, on the FINAL state
        self._write_working_tree(new); self._commit(message, actor)
        try:
            self._push()                     # only network op on the happy path
        except NonFastForward:               # external/human edit slipped in (rare)
            self._fetch(); self._reset_to_remote(); self._cache[name] = reload(name)
            continue                          # re-read, re-apply, re-validate, retry
        self._cache[name] = to_project(new)  # write-through: readers now up to date
        return MutationResult(before=current, after=new, ref=head_sha)
    raise ConcurrencyError                    # bounded, surfaced, never silent
```
On push failure that cannot be resolved, `reset --hard origin/main` to discard the local commit (clean
rollback) and raise; the cache is only updated after a successful push. On startup, `fetch` +
`reset --hard origin/main` (we only ever ack pushed writes, so discarding local-only state is safe).

`reconcile`: `fetch`, `git diff --name-only <old>..<new>` for changed paths, re-read only changed files
into the cache. Driven by a Forgejo push webhook (immediate) plus a slow fallback poll. Replaces the 30s
full-clone refresh. Because ZAD writes are write-through, reconcile usually finds nothing.

`history` / `read_at` / `previous`: local `git log` / `git show` on the warm copy. Fast, no network.

## Concurrency and ACID

The guarantee under heavy same-file mutation (components added, deployments changed within seconds):

1. **In-process (99%): the per-repo `asyncio.Lock`.** Same-project mutations serialize; each waiter
   re-reads the previous winner's result and applies on top. No rebase needed for in-process contention.
2. **Mutations are functions, applied under the lock, on freshly-read state.** This is the one
   behavioral change from today (callers pass a `change` fn, not a pre-built dict). It removes the
   read-early/write-late TOCTOU window that causes lost updates. Existing manager methods
   (`add_component`, `update_component`) already operate on current `project_data`, so they wrap naturally.
3. **Cross-cluster / external / human-edit (1%): optimistic git as compare-and-swap.** Push
   non-fast-forward = "HEAD moved"; fetch, re-read, re-apply the function, re-validate, push again
   (bounded retries + backoff). True semantic conflict (e.g. two adds of the same component) surfaces a
   clear error via the uniqueness check, never a silent bad merge. Never trust git text-rebase for
   correctness.

ACID scorecard (today -> after):
- Atomicity: validate-before-commit already guarantees invalid state never commits; add clean
  `reset --hard` rollback on push failure.
- Consistency: keep schema+structural pre-commit; add post-rebase re-validation.
- Isolation: today only optimistic push; add the per-repo lock -> serializable within a cluster,
  optimistic push across clusters.
- Durability: push stays the ack; startup reset-to-remote for crash recovery.

**Multi-replica future (do not build now):** swap the in-process lock for a Postgres advisory lock
(`pg_advisory_xact_lock(hashtext('project:'+name))`, per-cluster shared resource) or a single-writer
leader (funnel mutations through one task worker). Interface unchanged.

## Caching strategy

- Now: full parsed YAML per project in memory (files are small). `get_all` for the overview and
  `get`/`get_decrypted` for view/edit/API are all instant.
- If files grow large later: keep a lightweight summary (name, display name, status, members) always in
  memory for the overview, lazy-load full data per project with an LRU. This is internal to
  `GitProjectStore` and changes no caller and no interface.

## Consolidation map (current -> new)

| Today (scattered) | Becomes |
|---|---|
| `ProjectService` in-memory read cache | the cache inside `GitProjectStore` |
| `ProjectManager.get_contents` (clone-to-read) | `store.get` / `store.read_at` |
| `save_and_commit_project` / `mutate_and_commit_project` | `store.create` / `store.mutate` |
| project-files git connector (per-request clone) | one warm, reused working copy inside the store |
| `ensure_projects_fresh` 30s full clone | `store.reconcile` (incremental fetch + webhook) |

`ProjectManager` keeps its orchestration (deploy, Argo, namespaces, DB, Keycloak, MinIO) but for
project-file access calls `store`. Add a CI guard forbidding new direct git access to project files, so
"one responsible class" stays true. Sweep the extra project-files-connector call sites during Phase 1
(`restore_router`, `resource_tuning_service`, `startup`) and route each onto the store; surface anything
that needs raw repo access for a non-project-file reason instead of force-fitting it.

## Reconciliation model (change detection: the diff must keep working)

The existing "diff the project file against a previous version to detect and act on changes (e.g. a
service removed)" must keep working. Two independently-shippable pieces:

### Piece A: fix the diff baseline (safe removal detection)

Today the code diffs `HEAD` against the *immediately previous commit* of the file. That is unsafe:
intermediate commits hide deltas, so a removal can be missed under bursts/crashes/skips ("works if not
too much action"). Fix: **diff the current file against the last SUCCESSFULLY PROCESSED version**, not
the previous commit.

- `delta = compare(current_file, last_processed_file)` is the cumulative delta since the last successful
  run, so any removal in that window is always included, regardless of how many commits batched.
- The checkpoint advances **only on success**, so a failed/skipped/crashed run does not lose the delta.
- Uniform for ZAD edits and manual git edits: the process always compares current vs last-processed.
- You do NOT process each commit. You process the *latest* state; the checkpoint is only the comparison
  baseline. Superseded churn cancels out.

Checkpoint storage: reuse the `runs` concept - the commit of the last successful run for a project is the
checkpoint; reconstruct its content via `store.read_at(name, that_commit)` on the warm copy (local, fast).
Alternatively store the last-processed YAML snapshot in a Postgres row (self-contained, no git dependency).

No ownership markers on resources are required for A: the *old file version* tells you exactly what the
removed thing was, and deterministic naming derives its resources to delete.

Honest boundary: A guarantees detection of every *file* change since the last successful process (the
stated requirement). It does NOT catch cluster drift that is not reflected in the file (e.g. someone
deleted a live resource the file still declares). That needs Piece C.

### Piece B: partial / deferred processing (NOT a small change; two separate concerns)

This is bigger than it looks, and it is really two independent things. Both are future work.

**B1: impact-classified processing (change something without deploying it).**
The delta says exactly which fields changed, so route work by impact:
- Lightweight changes (attachment content, description, member list): run only the lightweight handler
  (e.g. write the attachment secret, update metadata), skip manifest regen / Argo.
- Deployment-affecting changes (components, images, resources, services, domains): run the full path,
  scoped to only the affected deployment(s).
Goal: e.g. add an attachment to the project file without it being deployed, and cut the "reprocess
everything on every edit" cost. This requires a per-field impact map and scoped-per-deployment reconcile.

**B2: an explicit "skip processing / defer" flag on API tasks (separate issue, for later).**
A cross-cutting flag on API mutation operations that says "just update the project file, do not process
or deploy it now". This lets you make many changes in bulk (build up a project via several API calls) and
then trigger one process at the end. This is its own issue, separate from B1, and applies to *all* API
tasks, not only attachments.

Note how B2 composes cleanly with Piece A: because A diffs the current file against the last
*successfully processed* checkpoint, all the deferred/bulk changes simply accumulate in the file and get
processed together, correctly, on the next explicit process run. So A is the enabler; B2 is the API-level
control on top of it. Build A first; B1 and B2 are independent follow-ups.

### Piece C: level-triggered reconcile (future backstop; needs ownership markers)

The *guarantee* that a removal is always eventually acted on comes from level-triggered reconciliation:
compare desired (current file) vs actual (resources enumerated by ownership label), delete actual-minus-
desired. History-independent, immune to batching/crashes/skips. Requirements (each is a testable
invariant; the guarantee is only as strong as the weakest):
1. **Ownership stamping** by construction: every resource ZAD creates (K8s + Keycloak + Postgres + MinIO +
   DNS) is tagged `(project, component)` via a single creation helper, so orphans are discoverable.
2. **Deterministic desired** set from the file.
3. **A registry of every managed kind/system** the sweep iterates (a missing kind = an uncatchable
   deletion).
4. **Converge fully**, on a schedule and on demand.
Kubernetes' own model: events for responsiveness (the diff, Piece A/B) + periodic full resync for
correctness (Piece C). The diff may make you faster; it must never be the reason something is correct.
Coverage test: for each managed thing, remove it from the file, run reconcile, assert the resource is gone.

## Smooth, cancellable processing (future; addresses the 300s block)

Today processing serializes per deployment with a synchronous health-gate wait (up to 300s), so a new
change queues behind a stale, slow one. Target: a **per-deployment, coalescing, cancellable reconcile
queue** (Kubernetes-controller shape):
- **Coalesce (latest-wins):** rapid commits for one deployment collapse to a single "reconcile to latest".
- **Preempt, do not roll back:** if a newer desired supersedes a reconcile waiting on an Argo sync, cancel
  the *wait* and re-reconcile to latest; idempotency makes this safe (the next reconcile converges Argo to
  the newest manifests). Do not undo the in-flight sync.
- **Make the health wait a cancellation point.**
- **Scope per deployment** so A's slow sync does not block B.
- **Bigger lever:** decouple apply from health - push manifests, record "syncing", observe health
  asynchronously instead of holding a worker for 300s; keep synchronous gating only where ordering
  genuinely requires it, and make even that cancellable.

## Phasing (each stage independently shippable)

1. **ProjectStore + warm copy + lock + mutation-as-function** (`store.get/create/mutate/delete`,
   reconcile replacing the 30s clone) **and Piece A** (checkpoint-diff). Behavior-preserving, biggest
   latency win, closes the ACID gap. Route all callers through the store + CI guard. **This is the agreed
   next build.**
2. **Piece B1** (impact-classified partial processing: change/attach without deploying, per-deployment
   scoping) and **Piece B2** (the "skip processing / defer" flag on API tasks for bulk edits). Independent
   follow-up issues; B2 composes with A. Not small; scope each on its own.
3. **Coalescing + cancellable per-deployment scheduler**, then optional async health observation.
4. **Future:** multi-replica lock (pg-advisory/leader), Piece C (ownership-marker level-triggered
   reconcile), and only if a shared cross-cluster store ever exists, a `DatabaseProjectStore` behind the
   same interface.

## Verification

- Unit: `mutate` serializes concurrent same-project calls (fire N coroutines, assert all N changes land,
  no lost update); validate-before-commit (invalid change never commits); reset-on-push-failure rollback.
- External-edit: simulate a remote push between read and push; assert fetch+re-apply+re-validate+retry.
- Reconcile: an external change to a file appears in the cache after `reconcile()` / webhook.
- Piece A: batch several edits (including a removal) between two "process" runs against a stale checkpoint;
  assert the removal is detected.
- Latency: benchmark `get` (target sub-ms from cache) and `mutate` (target bounded by one push, tens of
  ms, flat vs repo history) against today's clone-per-op baseline.
- Piece C (when built): remove-each-managed-thing coverage suite.

## Non-goals / explicit rule-outs

- DB as source of truth (no shared cross-cluster DB; git stays authoritative).
- Routing project-file access through Argo (Argo consumes generated manifests, not definitions).
- Per-project git repos / per-project checkout dirs (fragments the single-repo audit history; the
  per-project unit is an in-memory cache entry).
- Shallow-only clone (breaks the removed-component diff; the warm copy keeps full history locally).
