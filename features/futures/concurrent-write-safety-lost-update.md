# Concurrent project-file writes lose updates (snapshot save vs mutator save)

> **Status: REAL BUG, still not fixed. First diagnosis below was WRONG — see the
> correction.** The reallife concurrency E2E (`test_ui_env_vars_while_api_patches`,
> `test_ui_removal_while_api_patches`, + downstream `test_final_state`) fails live.

## CORRECTION (investigated 2026-08-03) — the naive "no base" diagnosis was wrong

The original root cause below ("most write paths snapshot-save without base") is
**incorrect**. The concurrency machinery is already in place and the write paths
already use it:

- `save_and_commit_project` already passes `base` to the store, falling back to
  `self.__contents_as_read` (project_manager.py:1693). `get_contents()` sets that
  to a **pristine `copy.deepcopy`** (project_manager.py:6476), so the base is a
  clean pre-edit snapshot even without an explicit argument.
- `get_project_store()` is a **process-wide singleton** with one asyncio lock and
  one warm working copy; the task worker runs **in-process** (server.py:162,
  "combined mode"), so web-request writes and API-task writes share that lock and
  serialize.
- `ProjectStore.save` already does a base-aware compare-and-swap with a validated
  **3-way merge** (`_reconcile_with_concurrent_write` → `_apply_our_change_to`),
  raising `ConflictError` (never silent last-writer-wins) when it can't merge.

So adding an explicit `base=copy.deepcopy(...)` to the two saves (commit b5488b05)
was a **no-op** — the value was identical to the existing fallback — and it did
NOT fix the tests (reverted in 3aed68a9). The lost update is NOT a missing base.

**What the live evidence actually shows** (reallife run, HEAD with the no-op fix):
`beta`'s UI removal and the API image-update both get overwritten, AND crucially
**no `three-way merge` / `changed since it was read` log fires**, and there is **no
push-conflict on `zad-projects`** (the observed rebases are all on
`zad-deployments`/`zad-argo`). That means the store's reconcile sees
`current == base` for the second writer — the first writer's committed change is
not visible when the second reconciles — so both commits fast-forward and one
silently wins. The reconcile machinery is correct; it just isn't being triggered.

**Prime suspects to investigate next (with the user):**
1. **A UI edit fires more than one save.** `router_detail_edit` does the store-save
   (`_process_and_save_modal_edit`, line ~1533) AND then starts a deployment-process
   task (line ~1342) carrying `state.base_version` captured at *form load*. That
   task re-saves the project; if its base is the pre-edit version, its save can
   re-publish the pre-edit state over a concurrent change. Map every save one UI
   edit triggers before touching the store.
2. **Warm-copy read-vs-committed visibility gap.** Confirm (temporary logging in
   `_reconcile_with_concurrent_write`) whether reconcile runs during the race and
   what `current`/`base` it actually sees — does the warm copy have the first
   writer's commit when the second reconciles under the lock?
3. Only after that: decide whether the real fix is threading `base_version`
   correctly through the deployment-process task, collapsing the double-save, or
   moving these paths to `mutate_and_commit_project` (single serialized RMW).

The sections below are the ORIGINAL (wrong) analysis, kept for history.

---

## Symptom (reproduced live)

`tests/e2e/test_sandbox_reallife.py` fires a UI edit and an API patch at the **same
project file** concurrently and asserts both survive. On the sandbox they fail:

- `test_final_state_of_all_projects`: `components ['alpha','beta','gamma','web'],
  expected ['alpha','gamma','web']` — a UI **removal of `beta` was lost** (beta
  reappeared), and `web`'s image patch was lost too.
- `test_ui_removal_while_api_patches_same_file`,
  `test_ui_env_vars_while_api_patches_same_file`: the expected end state never
  lands in git within `GIT_TIMEOUT`.

(These tests only started actually *running* once the wizard-state-leak was fixed;
before that they errored at setup, so this guarantee was never really verified —
the platform does not currently meet it.)

## Root cause

There are two ways to persist a project file:

1. **Snapshot** — `ProjectManager.save_and_commit_project(project_data, msg)`. The
   caller reads the file, mutates a dict **in memory**, and hands the whole
   finished dict to be written. It has no way to re-apply a *semantic* change onto
   a newer base: on a conflict it can only re-push the same stale snapshot.
2. **Mutator** — `ProjectManager.mutate_and_commit_project(mutator, msg)` →
   `ProjectStore.mutate`. The caller passes a **function** that transforms fresh
   data. The store holds the per-repo lock for the whole read-modify-write (so
   in-process writers serialize, each re-reading the previous winner) and falls
   back to fetch/re-apply/retry for external writes. This is concurrency-safe.

Almost every write path uses the **snapshot** form — including the config/patch and
add/remove-component task path (`opi/core/task_handlers_project.py:172`) and the
API routes (`opi/api/router.py`, `resource_router.py`) — while
`mutate_and_commit_project` is used in only a couple of places
(`delete_project_manager.py`). So when a UI edit and an API patch race:

- both read the same base (with `beta`),
- UI writes a snapshot **without** `beta`,
- API writes a snapshot **with** `beta` + new image (its read predated the removal),
- the second snapshot clobbers the first → the removal is lost.

The per-repo lock does **not** save the snapshot path: it serializes the *writes*,
but each snapshot was already computed from a stale read, so the later writer
overwrites the earlier winner with old data. Only the mutator path re-reads inside
the lock and re-applies, which is why it is safe.

## Fix (the real work)

Move the mutating write paths from `save_and_commit_project(data)` to
`mutate_and_commit_project(mutator)`, expressing each edit as a function of fresh
data instead of a precomputed snapshot:

- add-component / remove-component / patch-component (the CONFIGURE/patch task in
  `task_handlers_project.py` and the API routes that mutate components/services),
- the UI detail-edit apply path (`opi/web/router_detail_edit.py`),
- env-var edits, resource patches, service config writes.

Each becomes `mutate_and_commit_project(lambda data: apply_this_edit(data))`, so a
competing write just makes the store re-run the mutator on the latest state. Pure
read-then-write-whole-snapshot callers that legitimately own the entire file
(restore, backup) can stay on the snapshot form.

### Considerations

1. **Idempotent mutators.** Each mutator must be safe to re-run (return `None` when
   the change is already present — e.g. removing a component that a prior winner
   already removed) so re-apply converges.
2. **Scope of change.** This touches many call sites; do it path-by-path with the
   reallife concurrency tests as the gate, not in one sweep.
3. **Validation.** `mutate_and_commit_project(enforce_validation=True)` runs the
   same structural validation, so per-path conversion keeps the safety net.
4. **Serialize UI + API?** Alternative/complement: route UI saves through the same
   task worker so all writes share one ordered queue. Bigger change; the mutator
   conversion is the smaller, correct first step.

## Where to start

Convert the two paths the failing tests exercise first — the CONFIGURE/patch task
(`task_handlers_project.py:172`) and the UI detail-edit apply
(`router_detail_edit.py`) — then run
`tests/e2e/test_sandbox_reallife.py -m "e2e and sandbox"` to confirm the
lost-update tests pass, then extend to the remaining mutating callers.
