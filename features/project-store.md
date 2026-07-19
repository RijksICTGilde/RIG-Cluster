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
| Working copy | One warm, long-lived clone per process, full history (blobless). Never deleted. |
| Write serialization | A per-repo `asyncio.Lock`. Same-project writers queue; each re-reads the previous winner's result. |
| Mutations | A *change function* applied to freshly-read state, validated on the FINAL state, then committed and pushed. |
| External writes | A non-fast-forward push means HEAD moved: reset to remote, re-read, re-apply, re-validate, retry (bounded). |
| Durability | The push is the ack. The cache is updated only after a successful push. |
| Rollback | A failed push hard-resets the warm copy, so no local commit lingers. |

The interface is backend-agnostic: nothing git-specific leaks out (`ref` is an opaque string, not a
commit SHA), so a `DatabaseProjectStore` could replace `GitProjectStore` without touching callers.

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

## Freshness

- `store.bootstrap()` — startup: clone the warm copy and load every project into the cache.
- `store.reconcile()` — fetch, diff the changed paths, re-read only the changed files. Called by
  `ensure_projects_fresh()` when the 30s TTL expires. Because ZAD's own writes are write-through,
  this normally finds nothing and costs one fetch.

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
  `ProjectManager` marks it not-owned so it is never closed.

A CI guard (`tests/test_single_path_enforcement.py::test_no_direct_projects_repo_clones_outside_the_store`)
fails the build if a new direct clone of `zad-projects` is added under `opi/api`, `opi/web`,
`opi/core` or `opi/services`.

## Not yet done

- `opi/api/restore_router.py` still opens its own clone in three read-only restore paths
  (allowlisted in the CI guard, with the reason).
- Piece A of the design (diffing against the last *successfully processed* version instead of the
  previous commit) is not implemented. Change detection still uses the previous-commit baseline via
  `get_previous_file_content`. `store.read_at` and `MutationResult.ref` are the enablers for it.
- Pieces B and C (impact-classified partial processing, deferred-processing flag, level-triggered
  reconcile with ownership markers) are future work by design.

## Files

- `opi/services/project_store.py` — interface, dataclasses, `GitProjectStore`
- `opi/manager/project_validation.py` — shared schema/structural validation
- `opi/connectors/git.py` — `show_file_at`, `list_file_revisions`, `list_changed_files`
- `tests/test_project_store.py` — concurrency, validation, rollback, retry, reconcile, history
