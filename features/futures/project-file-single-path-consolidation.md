# Project-File Single-Path Consolidation — Work-List

Status: PLAN (not started). Branch base: `claude/attachments`.

## Why

Today the project file has many load paths and many save/commit paths, several
divergent YAML dumpers, an in-memory cache that is only reliably refreshed at
startup, and — most dangerously — **API mutation endpoints that bypass the
schema + structural validation the UI enforces**. An API call can therefore
commit a malformed or structurally broken project file to `zad-projects`
(validation only runs *after* commit inside `process_project_from_git`,
project_manager.py:2227). This work consolidates everything onto one validated
load path and one validated save path owned by `ProjectManager`.

Already landed this session (point fixes, NOT the structural work): `delete_component`
and `remove_attachment` are `ProjectManager` methods; subdomain-approval and modal-edit
submit read fresh via `get_contents`; `ServiceListConverter` restores catalog only at
project scope.

---

## Target architecture (the contract every item must converge on)

### Single LOAD path

`ProjectManager.get_contents()` (project_manager.py:5950) is the ONLY load path
for callers that intend to mutate. It reads fresh from disk via
`_project_file_handler.read_project_file()` which auto-migrates in memory
(project_file_handler.py:338). The in-memory `project_service` cache
(`get_project(name).data`, project_service.py:127) is **read-only**, for display
and lookups only; it is NEVER serialized back to git by a mutation handler.

Removed/redirected: `ProjectManager.get_project_data()` @deprecated
(project_manager.py:7147, bare `YAML()`, no migration);
`resource_tuning_service.get_project_data_from_git()` (resource_tuning_service.py:98,
no migration) and `get_project_data()` (resource_tuning_service.py:70);
`api/resource_router._get_project_data()` (resource_router.py:40).

### Single SAVE path (with mandatory validation)

New method on `ProjectManager`:

```python
async def save_and_commit_project(
    self,
    project_data: dict[str, Any],
    commit_message: str,
    *,
    refresh_cache: bool = True,
) -> None:
    """The ONLY way to persist a mutated project file.

    1. validate_project_schema(project_data)            # JSON schema — raises ProjectSchemaError
    2. self._validate_structural_integrity(project_data)# refs/uniqueness/domain — raises
    3. save_yaml_to_path(full_path, project_data)        # canonical dumper only
    4. await git_connector.commit_and_push(commit_message)
    5. if refresh_cache: project_service.load_project_from_data(project_data, filename)
    Steps 1-2 happen BEFORE any disk write or commit. Fails closed.
    """
```

- Lives in `project_manager.py`. Uses the existing `commit_and_push(message)`
  (git.py:1101) and `save_yaml_to_path` (yaml_util.py:57, canonical writer).
- `validate_project_schema` already exists (project_schema.py:41).
- `_validate_structural_integrity` collects the reference/uniqueness/domain
  checks currently scattered inside `add_component` (project_manager.py:6436/6445/6456/6484/6498),
  `add_component_to_deployment` (6724/6739/6754/6777), `upsert_deployment`
  (6101/6188 `_enforce_domain_config`) into ONE reusable method run against the
  final merged dict, so it holds no matter which caller produced the dict.

### Cache-refresh rule

The central save is the ONLY place that refreshes the cache after a mutation
(`project_service.load_project_from_data`, project_service.py:244). No handler
refreshes the cache on its own anymore. Startup population
(`refresh_projects_from_git` → `replace_all_projects`, project_service.py:189)
is unchanged.

### Validation-enforcement rule (first-class outcome)

Because validation lives INSIDE `save_and_commit_project`, every caller — API
and UI alike — validates schema + structural integrity before commit, by
construction. No mutation endpoint may call `commit_and_push` / `create_or_update_file(do_commit_and_push=True)`
on project-file content directly. CI guard added in Phase 5 to keep it that way.

### Single dumper

`yaml_util._create_yaml_writer()` (yaml_util.py:17) is canonical. All ad-hoc
`ruamel.yaml.YAML()` / stdlib `yaml.dump()` sites that emit PROJECT data are
replaced by `dump_yaml_to_string()` (yaml_util.py:139) or `save_yaml_to_path()`.

---

## Phase 1 — Establish the central load + save + validation on ProjectManager

> All Phase-1 work is in `project_manager.py` + one new helper. Single owner /
> single worktree (heavy collision surface). Do NOT parallelize within Phase 1.

### 1.1 Add `_validate_structural_integrity(project_data)` to ProjectManager
- File/func: `opi/manager/project_manager.py` — new private method.
- Change: extract the reference/uniqueness/domain checks now embedded in
  `add_component` (6436/6445/6456/6484/6498), `add_component_to_deployment`
  (6724/6739/6754/6777), `upsert_deployment` (6101, `_enforce_domain_config` 6188)
  into one method that operates on the final merged dict and raises a typed error
  (reuse/extend `ProjectSchemaError` or a new `ProjectIntegrityError`).
- Leave the inline checks in place for now (they still guard their own early
  returns); 1.2 will route the final dict through this too.
- Dependencies: none. **Blocks 1.2.**

### 1.2 Add `save_and_commit_project(...)` to ProjectManager
- File/func: `opi/manager/project_manager.py` — new method (contract above).
- Order: `validate_project_schema` → `_validate_structural_integrity` →
  `save_yaml_to_path` → `commit_and_push` → conditional `load_project_from_data`.
- Reuse existing migration-save block as the model (project_manager.py:2233-2236).
- Dependencies: **1.1**. **Blocks all of Phase 2.**

### 1.3 Make `save_project_data()` delegate (keep signature, change body)
- File/func: `project_manager.py:1288` `save_project_data()`.
- Change: currently local-write only (`save_yaml_to_path(path, get_contents())`).
  Repoint internal mutators to `save_and_commit_project`. Either (a) make
  `save_project_data` raise/deprecate so callers must move, or (b) keep it as a
  local-only write but audit each of its 10 call sites
  (project_manager.py:6201/6375/6557/6604/6640/6685/6796/6855/6912/7041) to ensure
  each is followed by a commit that now goes through `save_and_commit_project`.
  Prefer (a): replace the local-only saves inside add/remove/update component &
  service methods so the method builds the final dict then calls
  `save_and_commit_project` once. This is what closes the API validation gap at
  the source.
- Dependencies: **1.2**. **Blocks 2.x API items that rely on these managers.**

**Phase 1 verification:**
```bash
cd operations-manager/python
uv run ruff check opi/manager/project_manager.py --fix && uv run ruff format opi/manager/project_manager.py
uv run pyright opi/manager/project_manager.py
uv run pytest tests/ -k "project_manager or schema or structural" -q --tb=short
```
Add a unit test that calls `save_and_commit_project` with a schema-invalid dict
and asserts `ProjectSchemaError` is raised AND `commit_and_push` was never called
(mock the git connector).

---

## Phase 2 — Route every mutation handler through the central path

> Items 2.1–2.4 touch DIFFERENT files and can run in PARALLEL once Phase 1 lands,
> EXCEPT where noted. `web/router.py` and `web/router_detail_edit.py` are each
> touched by more than one concern — see Conflict Risks. Use a worktree per
> router file where two items would edit the same file.

### 2.1 API: components / deployments / services endpoints
- Files/funcs: `opi/api/router.py` — `upsert_deployment` (~993-1169),
  `add_component` (1178-1344), `add_component_to_deployment` (1353-1501),
  `add_service` (1510-1592); their async handlers in
  `opi/core/task_handlers_components.py` (`handle_add_component` 15-161,
  `handle_add_component_to_deployment` 163-304, `handle_add_service` 307-420).
- Change: these endpoints already delegate into `project_manager` methods that
  (after 1.3) call `save_and_commit_project`. Verify NO endpoint or task handler
  commits project YAML directly; remove any direct
  `commit_and_push` / `process_project_from_git`-before-validate ordering. Add the
  missing field validators for `add_service` (currently none — router.py:1577).
- Dependencies: **1.3**. Parallel with 2.2/2.3/2.4.

### 2.2 API: resource sanitize + restore endpoints
- Files/funcs: `opi/api/resource_router.py:238` (sanitize → `commit_project_yaml`);
  `opi/api/restore_router.py:779/790, 1221/1232, 1692/1704`
  (`save_project_file` + `commit_and_push`).
- Change: replace direct `commit_project_yaml` / `save_project_file`+`commit_and_push`
  with `project_manager.save_and_commit_project(...)`. Restore flows now get schema
  + structural validation before commit (currently none).
- Dependencies: **1.2**. Parallel.

### 2.3 UI: detail-edit, subdomain-admin, wizard save points
- Files/funcs:
  - `web/router_detail_edit.py` — `_modal_do_submit` save at 1350-1351
    (`save_project_file` + `load_project_from_data`) and `_commit_to_git` →
    `commit_project_yaml` (106-120); inline dump at 516.
  - `web/router_subdomain_admin.py:366-367` (`save_project_file` + `load_project_from_data`).
  - `web/router_wizard.py:1977-1978` (existing project save) and inline dump at 2013-2018.
  - `web/router.py:3084` (auto-tune `commit_project_yaml`); domain-settings
    commit at 2220-2232 (stdlib `yaml.dump` + direct commit) — see 2.5.
- Change: every one of these becomes a thin wrapper that builds the final dict
  and calls `save_and_commit_project`. Drop the now-redundant per-handler
  `load_project_from_data` calls (central save refreshes cache). Remove inline
  StringIO dumps (516, 2013-2018) — pass the dict to the central save, not a string.
- Dependencies: **1.2/1.3**. `router_detail_edit.py` collides with 2.4 → worktree.

### 2.4 Fix `handle_create_project` to validate before commit (CRITICAL gap #18)
- File/func: `opi/core/task_handlers_project.py:134` — `create_or_update_file(..., do_commit_and_push=True)`
  commits `yaml_content` verbatim with NO validation; `web/router_wizard.py:2013-2018`
  and `web/router_detail_edit.py:516` build that string.
- Change: stop passing a pre-serialized `yaml_content` string. Pass the project
  dict through the task payload, and in the handler call
  `validate_project_schema` + `_validate_structural_integrity` (or route through a
  `ProjectManager` so it uses `save_and_commit_project`) BEFORE the commit. New
  projects do not yet have a `ProjectManager` bound to a file path, so either
  (a) construct the file path + connector then call `save_and_commit_project`, or
  (b) expose a `validate_project_data(dict)` staticmethod and call it before
  `create_or_update_file`. Prefer (a) for true single-path.
- Dependencies: **1.2**. Touches `router_wizard.py` (collides w/ 2.3 wizard) and
  `router_detail_edit.py` (collides w/ 2.3) → coordinate / worktree.

### 2.5 UI: domain-settings endpoint (stale-cache + stdlib dumper + no validate)
- File/func: `web/router.py:1979` handler; reads fresh from git (2165), mutates,
  commits via stdlib `yaml.dump` + `create_or_update_file(do_commit_and_push=True)`
  (2220-2232), never refreshes cache (AT-RISK #4).
- Change: route through `save_and_commit_project` (gets validation + canonical
  dumper + cache refresh in one shot).
- Dependencies: **1.2**. Same file as 2.3 auto-tune + 2.1 — see Conflict Risks.

### 2.6 Background mutators: resource tuning, oom watcher, keycloak, delete
- Files/funcs: `services/resource_tuning_service.py:135` `commit_project_yaml`;
  `services/oom_watcher.py:374`; `manager/keycloak_manager.py:1751-1753`
  (`save_project_data` + `commit_and_push`); `manager/delete_project_manager.py:1632-1635`,
  333.
- Change: replace each with `save_and_commit_project`. NOTE: resource-tuning &
  oom writes are programmatic dicts that MUST still pass schema + structural
  validation — this is desirable (it stops the dp-bn7-class silent corruption).
  `commit_project_yaml` becomes a thin delegator or is removed (see 3.x).
  Delete-flow component removal (333) already goes through manager methods.
- Dependencies: **1.2**. Parallel (distinct files).

### 2.7 Load-path migration bugs (do alongside Phase 2; independent files)
- `services/resource_tuning_service.py:98` `get_project_data_from_git`: add
  `migrate_to_latest()` after the read, OR (preferred) replace the whole function
  with `ProjectManager.get_contents()` so it is migrated once and for all.
- `services/resource_tuning_service.py:70` `get_project_data` and
  `api/resource_router.py:40` `_get_project_data`: collapse into a single
  read-only cache accessor or `get_contents` per intent (mutators must use
  `get_contents`).
- Dependencies: none structurally, but coordinate with 2.2/2.6 which edit the
  same files → single owner for `resource_tuning_service.py`.

**Phase 2 verification:**
```bash
cd operations-manager/python
uv run ruff check opi/api opi/web opi/core opi/services opi/manager --fix
uv run pyright opi/api opi/web opi/core
uv run pytest tests/ -k "router or wizard or restore or resource or service" -q --tb=short
# Targeted: assert an API add_component with a schema-breaking payload returns an
# error and that the project file in the test git repo is UNCHANGED (no commit).
```

---

## Phase 3 — Remove / collapse duplicate load + save functions

> Pure deletion + delegation. Run AFTER Phase 2 so nothing still calls the old
> functions. Mostly parallel; watch shared files.

### 3.1 Remove deprecated `ProjectManager.get_project_data()` (7147)
- One caller: `federation_service.py:179` → repoint to `get_contents()`.
- Dependencies: Phase 2 complete for federation path.

### 3.2 Collapse `commit_project_yaml`
- `resource_tuning_service.py:135` `commit_project_yaml`: delete or make it a
  one-line delegator to `save_and_commit_project`. Confirm zero remaining callers
  after 2.2/2.3/2.5/2.6.
- Dependencies: 2.2, 2.3, 2.5, 2.6.

### 3.3 Audit `save_project_file` / `save_project_data` call sites
- `project_file_handler.py:3346` `save_project_file` and `yaml_util.save_yaml_to_path`
  stay as the low-level writer used INSIDE `save_and_commit_project`, but must no
  longer be called directly by mutation handlers. Grep and confirm only the central
  save + startup/backup paths call them.
- Backup writes (`core/backup_tasks.py:165, 477`) are local-only, non-git; leave
  them but confirm they do not feed a later commit that skips validation.
- Dependencies: 2.x complete.

**Phase 3 verification:**
```bash
cd operations-manager/python
grep -rn "commit_project_yaml\|get_project_data(" opi/ | grep -v "get_project_data_from_git\|def "   # expect ~0 mutation callers
grep -rn "do_commit_and_push=True" opi/ | grep -i project   # expect only central save
uv run pyright opi/
```

---

## Phase 4 — Unify the dumper

> Independent of Phases 2-3 in principle, but easier last (fewer moving call
> sites). Each file is a separate item; fully parallel.

Replace these PROJECT-data dump sites with `dump_yaml_to_string()` /
`save_yaml_to_path()` (canonical `_create_yaml_writer`, yaml_util.py:17):
- 4.1 `web/router_subdomain_admin.py:371` (missing indent/aliases) — likely
  removed entirely by 2.3.
- 4.2 `web/router_detail_edit.py:512` — likely removed by 2.3/2.4.
- 4.3 `web/router_wizard.py:2013` — likely removed by 2.4.
- 4.4 `web/router.py:2172/2220` (stdlib `yaml.dump`) — removed by 2.5.
- 4.5 `manager/project_manager.py:3351` (helm values) and `:3740` (helmfile values)
  — missing `preserve_quotes/width/indent/aliases`. These are NOT the project
  file but SOPS helm values; align to canonical writer to stop GitOps churn.
- 4.6 `utils/project_utils.py:441` (self-service project generation) — missing
  indent/aliases.
- 4.7 `handlers/configuration_handler.py:474` `to_yaml()` — missing indent/aliases.
- 4.8 `connectors/git.py` — confirm the bare `YAML()` instance is read-only; if it
  ever dumps project data, route through canonical.
- Note: 4.1-4.4 should be NO-OPS if Phase 2 already deleted those inline dumps;
  list them so the workset confirms removal rather than re-aligning dead code.
- Dependencies: 4.1-4.4 depend on the corresponding 2.x having landed.

**Phase 4 verification:**
```bash
cd operations-manager/python
grep -rn "ruamel.yaml import YAML\|yaml.dump(\|YAML()" opi/ | grep -v yaml_util.py   # review each remaining hit
uv run pytest tests/ -k "yaml or dump or roundtrip" -q --tb=short
# Round-trip a real project fixture through the central save and assert byte-stable
# output on a second save (no churn).
```

---

## Phase 5 — Regression tests + CI guard

### 5.1 Validation-bypass regression tests
- New tests asserting: every API mutation endpoint (`upsert_deployment`,
  `add_component`, `add_component_to_deployment`, `add_service`, sanitize, restore)
  REJECTS a schema-invalid and a structurally-broken payload, and leaves the git
  repo unchanged. Mirror with one UI flow (modal-edit) for parity.
- Reuse the dp-bn7 lesson: run `migrate_to_latest()` then validate on MIGRATED
  data, not raw.

### 5.2 Single-path enforcement guard
- A test (or ruff/grep CI step) that fails if any file under `opi/api`, `opi/web`,
  `opi/core`, `opi/services` (excluding `project_manager.py` central save) calls
  `commit_and_push`, `create_or_update_file(..., do_commit_and_push=True)`, or
  `save_project_file` on project content directly.

### 5.3 Cache-refresh test
- After a mutation through the central save, assert `project_service.get_project(name).data`
  reflects the new state (no stale window).

**Phase 5 verification:**
```bash
cd operations-manager/python
uv run pytest tests/ -k "single_path or validation_bypass or cache_refresh" -q --tb=short
uv run pytest tests/ -q   # full suite (pre-existing collection errors per operations-manager/CLAUDE.md are OK)
```

---

## Phase 6 — Sandbox verification

1. `task sandbox:setup` (or reuse running sandbox) and `task sandbox:skaffold-dev`
   (OPI on `localhost:9595`).
2. UI path: create a project via the wizard; edit a section via modal-edit;
   approve a subdomain. Confirm each commits exactly one well-formed file to the
   in-cluster Forgejo `zad-projects` repo and the project page reflects it
   immediately (cache refreshed).
3. API path: `POST /api/projects/{name}/components` with a VALID payload →
   committed + visible. Then with an INVALID payload (bad image / dangling
   service ref / schema violation) → 4xx, and `git log` in `zad-projects` shows
   NO new commit.
4. Negative dumper churn: edit one field via UI, confirm the git diff touches only
   that field (no whitespace/quote/wrap churn) — proves single canonical dumper.
5. Logs: `kubectl logs -n rig-system deployment/operations-manager -f` shows the
   `ProjectSchemaError` / integrity rejection on the invalid API call, before any
   commit log line.

---

## Conflict risks & worktree isolation

- **`opi/manager/project_manager.py`** — Phase 1 (1.1/1.2/1.3) AND Phase 4 (4.5)
  both edit it. Phase 1 is a single-owner sequential block; do 4.5 only after
  Phase 1 merges. Never parallelize edits to this file.
- **`opi/web/router.py`** — touched by 2.3 (auto-tune 3084), 2.5 (domain-settings
  1979-2232), 4.4. Assign ONE owner for `router.py` across these items, or stage
  them sequentially in one worktree.
- **`opi/web/router_detail_edit.py`** — touched by 2.3 (1350/106/516) and 2.4
  (516). Single owner / one worktree.
- **`opi/web/router_wizard.py`** — touched by 2.3 (1977) and 2.4 (2013). Single
  owner / one worktree.
- **`opi/services/resource_tuning_service.py`** — touched by 2.2, 2.6, 2.7, 3.2.
  Single owner.
- **Shared-checkout hazard (per MEMORY):** do NOT branch-switch in the shared
  checkout and do NOT `git stash`; use `git worktree` per parallel owner. Stage
  only your own hunks. Lint changed files manually (`ruff check <file> --fix`),
  never a bare pre-commit that auto-stashes other agents' work.

## Suggested parallelization

- Phase 1: 1 agent, sequential (1.1 → 1.2 → 1.3).
- Phase 2: up to 4 agents — A: 2.1 (api/router + task_handlers_components),
  B: 2.2 + 2.6 + 2.7 (resource/restore/oom/keycloak/delete + load bugs, owns
  resource_tuning_service.py), C: 2.3 + 2.4 + 2.5 (all web routers, one worktree
  because the three router files overlap), D: tests scaffolding for Phase 5.
- Phase 3: 1-2 agents after Phase 2.
- Phase 4: parallel per file, after the corresponding 2.x.
- Phase 5: 1 agent. Phase 6: 1 agent on the sandbox.

## Done criteria

- One load path (`get_contents`) and one save path (`save_and_commit_project`).
- `save_and_commit_project` validates schema + structural integrity BEFORE any
  write/commit, for EVERY caller; invalid files are never committed.
- No mutation handler commits project YAML directly (CI guard green).
- Cache refreshed only by the central save; no stale-cache overwrite window.
- One canonical dumper for all project data; no git churn on field edits.
- Regression + enforcement tests green; sandbox UI+API paths verified incl. the
  invalid-API-call-leaves-git-unchanged case.
