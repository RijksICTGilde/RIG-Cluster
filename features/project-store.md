# ProjectStore

Single authority for reading and writing project files (`zad-projects`, `projects/{name}.yaml`).

## What it does

Project definitions live as YAML in git, which stays the source of truth: it is the only resource
shared across clusters, it gives an audit history, and it stays human-editable. The ProjectStore
puts one class in front of that repo so reads are instant and writes are serialized and validated.

Before the store, every mutation built a fresh `ProjectManager`, which cloned the whole projects
repo to read authoritative state and cloned it again to write. A single edit cost roughly two
full-history clones plus a push, and that cost grew with the repo's history. There was also no
write lock: concurrency rested entirely on git's fetch+rebase+retry, and a rebase merges *text*
without re-validating the merged *semantics*, so two edits to the same file had a lost-update
window.

## How it works

| Concern | Behaviour |
|---|---|
| Reads | Served from the in-memory parsed cache (backed by `ProjectService`). No I/O. |
| Working copy | One warm, long-lived clone per process, full history (blobless). Never deleted. Used for **reads and history only**. |
| Writes | Built directly from git objects (`hash-object` -> private index -> `write-tree` -> `commit-tree`), never through the working tree. |
| Write serialization | A per-repo `asyncio.Lock`. Same-project writers queue; each re-reads the previous winner's result. |
| Mutations | A *change function* applied to freshly-read state, validated on the FINAL state, then committed and pushed. |
| External writes | A non-fast-forward push means HEAD moved: reset to remote, re-read, re-apply, re-validate, retry (bounded). Pushes use `allow_rebase=False` so divergence always reaches the store. |
| Durability | The push is the ack. The cache is updated only after a successful push, from what actually landed. |
| Rollback | A failed push moves the branch ref back. Nothing was written to disk, so there is nothing to clean up. |

### Why writes bypass the working tree

Writing into the shared warm copy and running `git add -A` was the source of four proven
data-loss paths, each verified on a live cluster against a real Forgejo:

- a commit for one project could carry another writer's half-written file, or a pending
  deletion, to the remote (`git add -A` stages everything in a *shared* tree);
- an uncommitted write could be silently discarded by a concurrent `reconcile()`
  (`reset --hard` + `git clean -fd` on a 30s TTL), after which the commit found nothing to
  commit and still reported success;
- a failed `delete()` left the removal staged, so the next unrelated commit published it;
- `push_changes` rebased internally and pushed the *merged* result unvalidated, so two
  individually-valid edits could merge into an invalid project file (a duplicated
  component), which then locked the project: every later edit failed validation.

Building the commit from objects with a per-operation index makes the first three
structurally impossible -- a commit cannot contain a path the store did not name -- and
`allow_rebase=False` hands divergence back to `mutate`, which re-applies and re-validates
before publishing. A genuine semantic conflict now surfaces as a clear domain error
("component 'web' is meervoudig gedefinieerd") instead of a corrupted file.

The interface is backend-agnostic: nothing git-specific leaks out (`ref` is an opaque string, not a
commit SHA), so a `DatabaseProjectStore` could replace `GitProjectStore` without touching callers.
Every read and every write goes through it -- there is no second door.

### One way in, for reads as well as writes

Reads used to go straight to `ProjectService` in 64 places, four of which also *wrote* to the
cache. A direct cache write is how the cache and git drift apart: readers then see a version
that is not in `zad-projects`. Startup made this worse by having two more loaders of its own.

That is all gone:

| Was | Is |
|---|---|
| `get_project_service().get_project(name)` | `get_project_store().get(name)` |
| `get_project_service().get_all_projects()` (dict) | `get_project_store().get_all()` (list) |
| `project_service.register/remove_project/replace_all_projects` | internal to the store |
| startup's own file walk + `refresh_projects_from_git` | `store.bootstrap()` + `store.reconcile()` |
| `ProjectManager.get_contents()` reading the working copy | `store.read_path()` |
| restore handlers cloning `zad-projects` themselves | `store.read_at()` |

`ProjectService` was three jobs in one class. The project cache stayed (now reached only through
the store), authorization moved to `opi/services/project_authorization.py` -- which reads through
the store like everything else -- and the platform-admin allowlist moved to `UserService`, next to
the email allowlist it already owned.

**Scope: the store owns `zad-projects` only.** The `zad-deployments` and `zad-argo-user-applications`
repos keep using their own `ProjectManager` git connectors and are explicitly out of scope.

## Usage

```python
from opi.services.project_store import get_project_store

store = get_project_store()

# Reads: instant, from cache
project = store.get("my-project")
all_projects = store.get_all()
decrypted = await store.get_decrypted("my-project")   # display/edit only, never written back

# Mutations: pass a change function, not a pre-built dict
def add_component(data: dict) -> dict | None:
    if any(c["name"] == "api" for c in data.get("components", [])):
        return None                     # already applied -> no-op, no commit
    data.setdefault("components", []).append({"name": "api", "type": "single"})
    return data

result = await store.mutate(
    "my-project", add_component, message="add component api", actor="user@example.com"
)
result.committed   # False when the change function returned None
result.before      # state before the change
result.after       # state after
result.ref         # opaque revision id of the resulting commit

# History
revisions = await store.history("my-project")
old = await store.read_at("my-project", revisions[-1].ref)
prev = await store.previous("my-project")   # file-scoped: last commit touching THIS path
```

### Why a change function and not a dict

`mutate` holds the lock for the whole read-modify-write. It reads the freshest committed state,
applies your function to it, and validates the result. A pre-built dict was assembled from state
read *earlier*, so two concurrent savers can still clobber each other. `store.save(...)` exists for
callers that still build the dict themselves (it gains the lock, validation, rollback and
write-through, but not the read-modify-write guarantee) — prefer `mutate` for anything that
reads then writes.

### Saving a pre-built dict: the compare-and-swap base

`store.save(...)` takes an optional `base` — the state the caller read before it built `data`. With
it, the save becomes a compare-and-swap: if the committed file no longer matches `base`, someone
wrote in between, and publishing `data` as-is would drop their change. The store then re-applies
the caller's change (`base` → `data`) on top of what actually landed, re-validates, and raises
`ConflictError` when that cannot be resolved.

`ProjectManager` supplies this automatically: `get_contents()` records what it handed out and
`save_and_commit_project()` passes it along, so the ~33 existing call sites gain compare-and-swap
without being touched.

One subtlety worth knowing before you add a helper to `ProjectManager`: projection helpers
(`get_name`, `get_deployments`, `get_components`, ...) read the project file too, and they run
*between* a caller's read and its save. They therefore read with `get_contents(record_base=False)`.
A projection that recorded would move the base to a state NEWER than the one the caller's dict was
built on, and the change re-applied against that newer base would revert a concurrent write instead
of merging with it — silently, because the result still validates. **If you add a helper that reads
the project file for its own use, pass `record_base=False`.**

### What the structural merge can and cannot resolve

The re-apply is a structural (parsed) merge, not a text merge, so two writers each appending a
component both survive — something `git merge-file`, `cherry-pick` and `rebase` all fail at, because
the two additions land on adjacent lines. It fails closed in these cases, raising `ConflictError`
rather than guessing:

| Situation | Outcome |
|---|---|
| Both changed the same existing field | conflict |
| We edited a field the other writer deleted | conflict |
| Both created the same previously-absent key with *different* values | conflict |
| Both created the same key with the *same* value | merges (agreement, not collision) |
| A concurrent removal earlier in a list | conflict, even when the edits are unrelated — deltas address list entries by index |
| Unrelated fields, or two appends | merges |

The third row needs its own check: deltas verify the previous value only for fields that already
existed, so a newly added key would otherwise be applied unconditionally and resolve silently to
whoever pushed second. See `_conflicting_added_keys`.

## Freshness

- `store.bootstrap()` — startup: clone the warm copy and load every project into the cache.
- `store.reconcile()` — fetch, diff the changed paths, re-read only the changed files. Called
  explicitly by the refresh action; it starts with an `ls-remote` check, so it costs nothing when
  the remote has not moved. Because ZAD's own writes are write-through, it normally finds nothing.
- The 30-second `ensure_projects_fresh()` TTL was removed: it tied
  freshness to how recently someone had opened a page (API-only consumers got none at all) and
  guaranteed nothing in particular. An edit made outside ZAD is an event, not something to
  rediscover on every render. It is picked up by an explicit refresh, or by a rejected push, which
  resyncs the whole tree and reloads every changed project into the cache.
- A slow fallback poll (`start_reconcile_poll`, every `PROJECT_STORE_RECONCILE_INTERVAL_SECONDS`,
  default 300s, `0` disables) calls `reconcile()` in the background. This bounds how long an
  out-of-band **revocation** — a member removed or an invite key revoked by pushing straight to
  `zad-projects` — can keep working on this instance. Explicit refresh and push conflicts only
  fire when someone acts; revocation must propagate even when nobody does. An idle tick is one
  `ls-remote` (~60ms, no object transfer). A transient error does not end the loop.

## Validation

Every write runs, on the final state, before anything is persisted:

1. `validate_project_schema` — JSON schema (`ProjectSchemaError`)
2. `validate_project_structure` — references, uniqueness, paths, root component, domains,
   attachments (`ProjectIntegrityError`)

Both live in `opi/manager/project_validation.py` so `ProjectManager` and `ProjectStore` run
identical checks. `enforce_validation=False` is for trusted narrow mutators (auto-tune, oom_watcher,
restore, keycloak config): the same checks still run and log, but pre-existing unrelated drift does
not block a recovery write. It is not a less-validated path.

## Constraints

- **Single replica per cluster.** The `asyncio.Lock` serializes writers *in this process*. Going
  multi-replica means swapping it for a Postgres advisory lock or a single-writer leader; the
  interface does not change.
- **Never close the warm connector.** `GitConnector.close()` deletes the working directory.
  `store.get_connector()` returns a shared connector that must outlive its callers.
  `ProjectManager` marks it not-owned so it is never closed. This is enforced by a test, not
  only documented: the invariant was written down here and broken anyway, when
  `get_project_data_from_git` changed from handing out a per-call clone to handing out the
  shared copy and one of its two callers was not updated.

Three CI guards in `tests/test_single_path_enforcement.py` fail the build on a regression:

- `test_project_cache_is_reached_only_through_the_store` -- any `get_project_service()` outside
  the store. Need project data? `get_project_store()`. Need to know whether a user may touch a
  project? `opi.services.project_authorization`. Need the admin allowlist? `UserService`.
- `test_no_direct_projects_repo_clones_outside_the_store` -- a new direct clone of `zad-projects`.
  The allowlist is empty: the store is the only code that clones that repo.
- `test_no_one_closes_the_stores_warm_connector` -- code closing a connector obtained from the
  store. Scoped per variable name, so a connector a module builds itself with `GitConnector(...)`
  may still be closed.

## Not yet done

- Piece A of the design (diffing against the last *successfully processed* version instead of the
  previous commit) is not implemented. Change detection still uses the previous-commit baseline via
  `get_previous_file_content`. `store.read_at` and `MutationResult.ref` are the enablers for it.
- Pieces B and C (impact-classified partial processing, deferred-processing flag, level-triggered
  reconcile with ownership markers) are future work by design.

## Files

- `opi/services/project_store.py` — interface, dataclasses, `GitProjectStore`
- `opi/manager/project_validation.py` — shared schema/structural validation
- `opi/connectors/git.py` — `show_file_at`, `list_file_revisions`, `list_changed_files`,
  `build_commit`/`set_branch_ref`/`sync_worktree_to_head` (the plumbing write path),
  `push_changes(allow_rebase=...)`
- `tests/test_project_store.py` — concurrency, validation, rollback, retry, reconcile, history
