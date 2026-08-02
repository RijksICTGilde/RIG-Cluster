# Concurrent project-file writes lose updates (snapshot save vs mutator save)

> **Status: REAL BUG, diagnosed, not fixed.** Root cause confirmed against the
> live sandbox by the reallife concurrency E2E suite. Fix is a broad, careful
> change across write paths — needs a deliberate decision, not an autonomous patch.

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
