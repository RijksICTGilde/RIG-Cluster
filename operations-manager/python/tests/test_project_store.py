"""Unit tests for GitProjectStore.

Covers the guarantees the store exists to provide:

* mutate serializes concurrent same-project calls -- all N changes land, no lost update
* mutations are applied to freshly-read state (read-modify-write, no TOCTOU window)
* validate-before-commit: an invalid final state never reaches a commit
* rollback: a failed push leaves no local commit behind
* external edits (non-fast-forward push) cause fetch + re-apply + re-validate + retry
* bounded retries surface a ConcurrencyError instead of failing silently
* reconcile pulls external changes into the cache incrementally
* history / read_at / previous read from the warm copy

The git layer is replaced by a fake connector that emulates a real remote (its own
HEAD, per-file history, and non-fast-forward rejection), so the store's own logic is
what is under test. The real GitConnector primitives are covered separately in
tests/test_git_store_primitives.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opi.core.project_schema import ProjectSchemaError
from opi.services.project_service import get_project_service
from opi.services.project_store import (
    ConcurrencyError,
    GitProjectStore,
    ProjectNotFoundError,
)
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

PROJECT_NAME = "demo"
RELATIVE_PATH = "projects/demo.yaml"


def _project(deployments: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """A minimal project dict that passes schema + structural validation."""
    data: dict[str, Any] = {
        "schema-version": 2,
        "name": PROJECT_NAME,
        "display-name": "Demo",
        "description": "test project",
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["odcn-production"],
        "services": [],
        "components": [],
        "deployments": deployments if deployments is not None else [],
        # The schema requires an AGE-encrypted (or base64+age) api-key.
        "config": {"api-key": "base64+age:dGVzdC1hcGkta2V5"},
    }
    data.update(extra)
    return data


class FakeRemote:
    """In-memory stand-in for the git remote: a HEAD plus per-commit file snapshots."""

    def __init__(self) -> None:
        self.commits: list[dict[str, Any]] = []
        self.files: dict[str, str] = {}

    @property
    def head(self) -> str:
        return self.commits[-1]["ref"] if self.commits else "0" * 40

    def commit(self, message: str, files: dict[str, str]) -> str:
        ref = f"{len(self.commits) + 1:040d}"
        self.files = dict(files)
        self.commits.append({"ref": ref, "message": message, "author": "tester", "files": dict(files)})
        return ref


class FakeGitConnector:
    """Emulates the GitConnector surface the store uses, against a FakeRemote.

    Tracks a local working tree and local HEAD separately from the remote, so a
    non-fast-forward push (remote moved since our base) is detected exactly the
    way real git would reject it.
    """

    def __init__(self, remote: FakeRemote, working_dir: str) -> None:
        self.remote = remote
        self._working_dir = working_dir
        self.tree: dict[str, str] = dict(remote.files)
        self.base_ref = remote.head
        self.local_head = remote.head
        self.pending_message: str | None = None
        # Commits built but not yet published, keyed by their ref. Mirrors real plumbing:
        # build_commit produces an object, set_branch_ref points the branch at it, and only
        # push publishes it.
        self._built: dict[str, tuple[dict[str, str], str]] = {}
        self._staged_commit: str | None = None
        # Test hooks
        self.fail_push_with: Exception | None = None
        self.on_before_push = None
        self.reset_count = 0

    async def get_working_dir(self) -> str:
        return self._working_dir

    async def ensure_repo_cloned(self) -> None:
        return None

    async def get_local_commit_hash(self) -> str:
        return self.local_head

    async def file_exists(self, path: str) -> bool:
        return path in self.tree

    async def delete_file(self, path: str) -> None:
        self.tree.pop(path, None)

    async def show_file_at(self, ref: str, path: str) -> str | None:
        if ref == "HEAD":
            return self.tree.get(path)
        for commit in self.remote.commits:
            if commit["ref"] == ref:
                return commit["files"].get(path)
        return None

    async def list_file_revisions(self, path: str, limit: int = 50) -> list[dict[str, str]]:
        revisions = [
            {"ref": c["ref"], "author": c["author"], "timestamp": "2026-01-01T00:00:00+00:00", "message": c["message"]}
            for c in self.remote.commits
            if path in c["files"]
        ]
        return list(reversed(revisions))[:limit]

    async def get_previous_file_content(self, path: str) -> str | None:
        touching = [c for c in self.remote.commits if path in c["files"]]
        if len(touching) < 2:
            return None
        return touching[-2]["files"].get(path)

    async def list_changed_files(self, old: str, new: str) -> list[str]:
        old_files = next((c["files"] for c in self.remote.commits if c["ref"] == old), {})
        new_files = next((c["files"] for c in self.remote.commits if c["ref"] == new), {})
        return sorted(set(old_files) ^ set(new_files) | {p for p in new_files if old_files.get(p) != new_files.get(p)})

    async def commit_and_push(self, message: str) -> None:
        if self.on_before_push is not None:
            hook, self.on_before_push = self.on_before_push, None
            hook()
        if self.fail_push_with is not None:
            error, self.fail_push_with = self.fail_push_with, None
            raise error
        if self.base_ref != self.remote.head:
            from opi.connectors.git import GitPushConflictError

            raise GitPushConflictError("non-fast-forward: remote has moved")
        ref = self.remote.commit(message, self.tree)
        self.base_ref = ref
        self.local_head = ref

    # -- plumbing write path -------------------------------------------------
    # The store no longer writes into the working tree. It builds a commit from
    # objects, points the branch at it, pushes, and only then syncs the checkout.

    def _tree_at(self, ref: str) -> dict[str, str]:
        """The committed tree at a ref -- NOT the working tree."""
        if ref in self._built:
            return dict(self._built[ref][0])
        return next((dict(c["files"]) for c in self.remote.commits if c["ref"] == ref), {})

    async def build_commit(self, changes: dict[str, str | None], message: str, parent: str | None = None) -> str | None:
        # Built from the PARENT COMMIT's tree, never from self.tree. Real plumbing
        # does read-tree <parent> into a private index and applies only the named
        # paths, so a dirty working copy cannot leak into the commit. Deriving this
        # from self.tree would reproduce the `git add -A` behaviour the store was
        # rewritten to eliminate, and no test would notice.
        parent_ref = parent or self.local_head
        parent_tree = self._tree_at(parent_ref)

        new_tree = dict(parent_tree)
        for path, content in changes.items():
            if content is None:
                new_tree.pop(path, None)
            else:
                new_tree[path] = content
        if new_tree == parent_tree:
            return None
        ref = f"built-{len(self._built)}-{self.local_head}"
        self._built[ref] = (new_tree, message)
        return ref

    async def set_branch_ref(self, commit: str, branch: str | None = None) -> None:
        # Moves the branch unconditionally, like `git update-ref`. Leaving local_head
        # alone for a freshly built commit would make the rollback assertions vacuous:
        # HEAD would never have moved, so they would hold whether or not the store
        # rolled back.
        self.local_head = commit
        self._staged_commit = commit if commit in self._built else None

    async def get_remote_commit_hash(self, branch: str | None = None) -> str | None:
        """What ls-remote would report: the remote's tip, without transferring objects."""
        return self.remote.head

    async def count_unpushed_commits(self, branch: str | None = None) -> int:
        if not self.remote.commits:
            return 0
        published = {c["ref"] for c in self.remote.commits}
        return 0 if self.local_head in published else 1

    async def push_changes(self, branch: str | None = None, max_retries: int = 5, allow_rebase: bool = True) -> None:
        if self.on_before_push is not None:
            hook, self.on_before_push = self.on_before_push, None
            hook()
        if self.fail_push_with is not None:
            error, self.fail_push_with = self.fail_push_with, None
            raise error
        if self.base_ref != self.remote.head:
            from opi.connectors.git import GitPushConflictError

            raise GitPushConflictError("non-fast-forward: remote has moved")
        assert self._staged_commit is not None, "push without a staged commit"
        tree, message = self._built[self._staged_commit]
        ref = self.remote.commit(message, tree)
        self._built[ref] = (tree, message)
        self._staged_commit = ref
        self.base_ref = ref
        self.local_head = ref

    async def sync_worktree_to_head(self) -> None:
        if self._staged_commit is not None and self._staged_commit in self._built:
            self.tree = dict(self._built[self._staged_commit][0])

    async def reset_to_remote(self, branch: str | None = None) -> None:
        self.reset_count += 1
        self.tree = dict(self.remote.files)
        self.base_ref = self.remote.head
        self.local_head = self.remote.head


class StoreHarness:
    """A GitProjectStore wired to a FakeGitConnector.

    The store builds commits from git objects rather than writing into a working tree,
    so nothing needs intercepting on the filesystem: the fake connector's ``tree`` is
    the committed state, and ``build_commit`` derives the next one from it.
    """

    def __init__(self, store: GitProjectStore, connector: FakeGitConnector, remote: FakeRemote) -> None:
        self.store = store
        self.connector = connector
        self.remote = remote


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path) -> StoreHarness:
    get_project_service().clear_all_projects()

    remote = FakeRemote()
    remote.commit("initial", {RELATIVE_PATH: dump_yaml_to_string(_project())})

    working_dir = str(tmp_path)
    connector = FakeGitConnector(remote, working_dir)
    store = GitProjectStore(working_dir=working_dir)

    async def _get_connector() -> FakeGitConnector:
        return connector

    monkeypatch.setattr(store, "get_connector", _get_connector)

    async def _list_project_files(_connector: Any) -> list[str]:
        return sorted(
            p.removeprefix("projects/") for p in connector.tree if p.startswith("projects/") and p.endswith(".yaml")
        )

    monkeypatch.setattr(store, "_list_project_files", _list_project_files)

    return StoreHarness(store, connector, remote)


def _current(harness: StoreHarness) -> dict[str, Any]:
    parsed = load_yaml_from_string(harness.remote.files[RELATIVE_PATH])
    assert parsed is not None
    return parsed


def _add_deployment(name: str):
    """A change function that appends a deployment, or no-ops if already present."""

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        deployments = data.setdefault("deployments", [])
        if any(d.get("name") == name for d in deployments):
            return None
        deployments.append({"name": name, "cluster": "odcn-production", "namespace": "demo", "components": []})
        return data

    return change


# ----------------------------------------------------------------------------
# mutate: serialization and read-modify-write
# ----------------------------------------------------------------------------


async def test_mutate_applies_and_commits(harness: StoreHarness) -> None:
    result = await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-1"), message="add dep-1", actor="tester")

    assert result.committed is True
    assert [d["name"] for d in result.after["deployments"]] == ["dep-1"]
    assert [d["name"] for d in _current(harness)["deployments"]] == ["dep-1"]
    # before/after carry the diff for downstream reconcile logic
    assert result.before is not None
    assert result.before["deployments"] == []


async def test_mutations_are_mutually_exclusive(harness: StoreHarness) -> None:
    """The lock guarantee: two mutations never run their read-modify-write concurrently.

    The change function yields to the event loop while "inside" the mutation. Without
    the per-repo lock a second mutate would enter at that point and max_active would
    exceed 1, which is exactly the read-early/write-late window that loses updates.
    """
    active = 0
    max_active = 0

    async def change(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)  # a real yield point inside the critical section
        active -= 1
        deployments = data.setdefault("deployments", [])
        deployments.append(
            {
                "name": f"dep-{len(deployments)}",
                "cluster": "odcn-production",
                "namespace": "demo",
                "components": [],
            }
        )
        return data

    await asyncio.gather(
        *(harness.store.mutate(PROJECT_NAME, change, message=f"m{i}", actor="tester") for i in range(5))
    )

    assert max_active == 1, f"mutations overlapped (max {max_active} concurrent); the per-repo lock is not holding"
    assert len(_current(harness)["deployments"]) == 5


async def test_concurrent_mutations_all_land_without_lost_update(harness: StoreHarness) -> None:
    """End-to-end: N concurrent same-project mutations all land, none is clobbered.

    Delivered by the lock (in-process serialization) plus the optimistic
    re-read/re-apply retry for anything that slips in from outside.
    """
    names = [f"dep-{i}" for i in range(8)]

    await asyncio.gather(
        *(
            harness.store.mutate(PROJECT_NAME, _add_deployment(name), message=f"add {name}", actor="tester")
            for name in names
        )
    )

    landed = sorted(d["name"] for d in _current(harness)["deployments"])
    assert landed == sorted(names), "a concurrent mutation was lost"


async def test_mutate_reads_freshest_state_not_caller_snapshot(harness: StoreHarness) -> None:
    """Each change function is applied to state read under the lock, not to a snapshot."""
    seen_bases: list[int] = []

    def change(data: dict[str, Any]) -> dict[str, Any]:
        seen_bases.append(len(data.get("deployments", [])))
        data.setdefault("deployments", []).append(
            {"name": f"dep-{len(seen_bases)}", "cluster": "odcn-production", "namespace": "demo", "components": []}
        )
        return data

    await harness.store.mutate(PROJECT_NAME, change, message="one", actor="tester")
    await harness.store.mutate(PROJECT_NAME, change, message="two", actor="tester")

    # The second call must have observed the first call's result (base of 1, not 0).
    assert seen_bases == [0, 1]


async def test_idempotent_noop_does_not_commit(harness: StoreHarness) -> None:
    await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-1"), message="add", actor="tester")
    commits_before = len(harness.remote.commits)

    result = await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-1"), message="add again", actor="tester")

    assert result.committed is False
    assert len(harness.remote.commits) == commits_before, "a no-op must not produce a commit"


async def test_mutate_unknown_project_raises(harness: StoreHarness) -> None:
    with pytest.raises(ProjectNotFoundError):
        await harness.store.mutate("does-not-exist", _add_deployment("x"), message="m", actor="tester")


# ----------------------------------------------------------------------------
# validation before commit
# ----------------------------------------------------------------------------


async def test_invalid_final_state_never_commits(harness: StoreHarness) -> None:
    """Schema validation runs on the FINAL state, before any write or commit."""
    commits_before = len(harness.remote.commits)

    def break_schema(data: dict[str, Any]) -> dict[str, Any]:
        del data["name"]
        return data

    with pytest.raises(ProjectSchemaError):
        await harness.store.mutate(PROJECT_NAME, break_schema, message="break it", actor="tester")

    assert len(harness.remote.commits) == commits_before
    assert "name" in _current(harness), "the invalid state must not have been persisted"


async def test_enforce_validation_false_still_persists(harness: StoreHarness) -> None:
    """Trusted programmatic mutators may proceed despite pre-existing drift."""

    def break_schema(data: dict[str, Any]) -> dict[str, Any]:
        del data["name"]
        data["deployments"] = []
        return data

    result = await harness.store.mutate(
        PROJECT_NAME, break_schema, message="recovery write", actor="auto-tune", enforce_validation=False
    )

    assert result.committed is True


# ----------------------------------------------------------------------------
# external edits, retry and rollback
# ----------------------------------------------------------------------------


async def test_external_push_triggers_reapply_and_retry(harness: StoreHarness) -> None:
    """A remote push between our read and our push must not lose either change."""

    def external_edit() -> None:
        data = _project(
            deployments=[{"name": "from-outside", "cluster": "odcn-production", "namespace": "demo", "components": []}]
        )
        harness.remote.commit("external edit", {RELATIVE_PATH: dump_yaml_to_string(data)})

    harness.connector.on_before_push = external_edit

    result = await harness.store.mutate(PROJECT_NAME, _add_deployment("mine"), message="add mine", actor="tester")

    assert result.committed is True
    assert harness.connector.reset_count == 1, "must reset to the remote before re-applying"
    landed = sorted(d["name"] for d in _current(harness)["deployments"])
    assert landed == ["from-outside", "mine"], "the external change must survive the retry"


async def test_conflict_then_winner_already_applied_is_idempotent(harness: StoreHarness) -> None:
    """If the concurrent winner already made our change, the retry is a clean no-op."""

    def external_edit() -> None:
        data = _project(
            deployments=[{"name": "mine", "cluster": "odcn-production", "namespace": "demo", "components": []}]
        )
        harness.remote.commit("external edit", {RELATIVE_PATH: dump_yaml_to_string(data)})

    harness.connector.on_before_push = external_edit

    result = await harness.store.mutate(PROJECT_NAME, _add_deployment("mine"), message="add mine", actor="tester")

    assert result.committed is False


async def test_exhausted_retries_raise_concurrency_error(harness: StoreHarness) -> None:
    """Bounded retries: surfaced as ConcurrencyError, never silently dropped."""

    def keep_moving_remote() -> None:
        harness.remote.commit("external churn", {RELATIVE_PATH: dump_yaml_to_string(_project())})
        harness.connector.on_before_push = keep_moving_remote

    harness.connector.on_before_push = keep_moving_remote

    with pytest.raises(ConcurrencyError):
        await harness.store.mutate(PROJECT_NAME, _add_deployment("mine"), message="add mine", actor="tester")


async def test_push_failure_rolls_back_local_commit(harness: StoreHarness) -> None:
    """A non-conflict push failure leaves no trace: the branch ref goes back to HEAD.

    Rollback is exact because the write never touched the working tree. The commit is
    built from git objects, so undoing it is just moving the ref back -- there is no
    half-applied file to reset away, and the unreferenced objects are collected later.
    """
    head_before = harness.connector.local_head
    harness.connector.fail_push_with = RuntimeError("remote unreachable")

    with pytest.raises(RuntimeError, match="remote unreachable"):
        await harness.store.mutate(PROJECT_NAME, _add_deployment("mine"), message="add mine", actor="tester")

    assert harness.connector.local_head == head_before, "the branch must not stay on the unpushed commit"
    assert _current(harness)["deployments"] == [], "nothing may be persisted on a failed push"
    assert harness.connector.tree == dict(harness.remote.files), "the working copy must still mirror the remote"


# ----------------------------------------------------------------------------
# cache write-through and reads
# ----------------------------------------------------------------------------


async def test_successful_mutation_is_written_through_to_cache(harness: StoreHarness) -> None:
    await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-1"), message="add", actor="tester")

    cached = harness.store.get(PROJECT_NAME)
    assert cached is not None
    assert cached.data is not None
    assert [d["name"] for d in cached.data["deployments"]] == ["dep-1"]
    assert harness.store.exists(PROJECT_NAME) is True


async def test_failed_mutation_leaves_cache_untouched(harness: StoreHarness) -> None:
    await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-1"), message="add", actor="tester")
    harness.connector.fail_push_with = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-2"), message="add 2", actor="tester")

    cached = harness.store.get(PROJECT_NAME)
    assert cached is not None
    assert cached.data is not None
    assert [d["name"] for d in cached.data["deployments"]] == ["dep-1"], "cache must reflect only durable state"


# ----------------------------------------------------------------------------
# history / read_at / previous
# ----------------------------------------------------------------------------


async def test_history_and_read_at(harness: StoreHarness) -> None:
    await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-1"), message="add dep-1", actor="tester")

    revisions = await harness.store.history(PROJECT_NAME)
    assert [r.message for r in revisions] == ["add dep-1", "initial"]

    original = await harness.store.read_at(PROJECT_NAME, revisions[-1].ref)
    assert original is not None
    assert original["deployments"] == []


async def test_previous_returns_version_before_head(harness: StoreHarness) -> None:
    await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-1"), message="add dep-1", actor="tester")
    await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-2"), message="add dep-2", actor="tester")

    previous = await harness.store.previous(PROJECT_NAME)

    assert previous is not None
    assert [d["name"] for d in previous["deployments"]] == ["dep-1"]


# ----------------------------------------------------------------------------
# reconcile
# ----------------------------------------------------------------------------


async def test_reconcile_pulls_external_change_into_cache(harness: StoreHarness) -> None:
    await harness.store.bootstrap()
    assert harness.store.get(PROJECT_NAME) is not None

    edited = _project(
        deployments=[{"name": "outside", "cluster": "odcn-production", "namespace": "demo", "components": []}]
    )
    harness.remote.commit("edited by a human", {RELATIVE_PATH: dump_yaml_to_string(edited)})

    await harness.store.reconcile()

    cached = harness.store.get(PROJECT_NAME)
    assert cached is not None
    assert cached.data is not None
    assert [d["name"] for d in cached.data["deployments"]] == ["outside"]


async def test_reconcile_without_new_commits_is_a_noop(harness: StoreHarness) -> None:
    await harness.store.bootstrap()
    head_before = harness.remote.head

    await harness.store.reconcile()

    assert harness.remote.head == head_before


async def test_reconcile_does_no_work_when_the_remote_has_not_moved(harness: StoreHarness) -> None:
    """The ls-remote check must short-circuit before the expensive part.

    Without it, every reconcile fetches and hard-resets the shared working copy
    while holding the write lock, for the overwhelmingly common case where the
    remote is exactly where we left it.
    """
    await harness.store.bootstrap()
    resets_before = harness.connector.reset_count

    await harness.store.reconcile()

    assert harness.connector.reset_count == resets_before, "reconcile reset the working copy for nothing"


async def test_reconcile_still_syncs_when_the_remote_moved(harness: StoreHarness) -> None:
    """And it must not short-circuit when there genuinely is something to pick up."""
    await harness.store.bootstrap()
    resets_before = harness.connector.reset_count

    changed = _project()
    changed["display-name"] = "handmatig gewijzigd"
    harness.remote.commit("external edit", {RELATIVE_PATH: dump_yaml_to_string(changed)})
    await harness.store.reconcile()

    assert harness.connector.reset_count > resets_before, "reconcile skipped a real remote change"


# ----------------------------------------------------------------------------
# timing observability: lock wait, hold and push duration are logged
# ----------------------------------------------------------------------------


async def test_mutation_logs_lock_and_persist_timing(harness: StoreHarness, caplog) -> None:
    """A successful mutation emits the wait/hold and push timing lines.

    These logs are how an operator sees whether throughput is limited by writes
    queueing on the single lock (wait) or by a slow git push (push). The exact
    durations are environment-dependent, so the test pins that the lines are
    emitted with the operation label, not their values.
    """
    import logging

    with caplog.at_level(logging.INFO, logger="opi.services.project_store"):
        await harness.store.mutate(PROJECT_NAME, _add_deployment("dep-timing"), message="timed", actor="tester")

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("store-lock mutate 'demo': held") for m in messages), messages
    assert any(m.startswith("store-push") and "push took" in m for m in messages), messages
    # The persist line states the outcome and the message it was for: without that,
    # a write that changed nothing is indistinguishable in production from one that
    # did, except by the presence of a store-push line beside it.
    assert any(m.startswith("store-persist") and "committed " in m and "timed" in m for m in messages), messages


async def test_self_heal_reset_reloads_externally_changed_projects(harness: StoreHarness) -> None:
    """The self-heal reset must not desync the cache from git.

    An interrupted rollback leaves an unpushed local commit. The next write's
    self-heal does fetch + hard reset, which does not only discard that local commit
    -- it fast-forwards the disk over every commit another writer pushed meanwhile.
    Those projects are then on disk but stale in the cache, and reconcile's fast path
    (remote head == local head) reads "nothing to do" forever. This is the exact
    production failure: a project served without its newest deployments until restart.
    """
    other_path = "projects/other.yaml"
    other_v1 = _project(name="other", deployments=[])

    # Remote starts with demo + other; load both into the cache.
    harness.remote.commit(
        "add other", {RELATIVE_PATH: dump_yaml_to_string(_project()), other_path: dump_yaml_to_string(other_v1)}
    )
    harness.connector.tree = dict(harness.remote.files)
    harness.connector.base_ref = harness.remote.head
    harness.connector.local_head = harness.remote.head
    await harness.store.bootstrap()
    assert harness.store.get("other") is not None
    assert [d["name"] for d in harness.store.get("other").data["deployments"]] == []

    # Another pod adds a deployment to 'other' -- the "pr-480" of the incident.
    other_v2 = _project(
        name="other",
        deployments=[{"name": "pr-480", "cluster": "odcn-production", "namespace": "other", "components": []}],
    )
    harness.remote.commit(
        "external: pr-480 on other",
        {RELATIVE_PATH: harness.remote.files[RELATIVE_PATH], other_path: dump_yaml_to_string(other_v2)},
    )

    # An interrupted rollback left an unpushed commit on the warm copy.
    harness.connector._built["built-orphan"] = (dict(harness.connector.tree), "orphan")
    harness.connector.local_head = "built-orphan"
    assert await harness.connector.count_unpushed_commits() == 1

    # A normal write to a DIFFERENT project triggers the self-heal.
    await harness.store.save(
        PROJECT_NAME, _add_deployment_result("d1"), message="unrelated write", actor="tester", enforce_validation=False
    )

    other = harness.store.get("other")
    assert other is not None
    names = [d["name"] for d in other.data["deployments"]]
    assert "pr-480" in names, "self-heal pulled pr-480 onto disk; the cache must reflect it, not a restart"


async def test_reconcile_repairs_cache_drift_even_when_disk_equals_remote(harness: StoreHarness) -> None:
    """The backstop: reconcile detects a cache that lags the disk, not just the remote.

    Gating reconcile on remote-head == disk-head assumes the cache matches the disk.
    If any path advances the disk without the cache (the self-heal reset was one such
    path), disk == remote holds while the cache is stale, and the old fast path read
    "nothing to do" forever. Tracking the head the cache reflects closes that: even
    with disk == remote, a cache behind that head is reloaded.
    """
    other_path = "projects/other.yaml"
    harness.remote.commit(
        "add other",
        {RELATIVE_PATH: dump_yaml_to_string(_project()), other_path: dump_yaml_to_string(_project(name="other"))},
    )
    harness.connector.tree = dict(harness.remote.files)
    harness.connector.base_ref = harness.remote.head
    harness.connector.local_head = harness.remote.head
    await harness.store.bootstrap()

    # An external commit lands (another cluster/pod), and the disk is fast-forwarded
    # onto it WITHOUT the cache -- simulating any drift path, not a specific bug.
    other_v2 = _project(
        name="other",
        deployments=[{"name": "pr-480", "cluster": "odcn-production", "namespace": "other", "components": []}],
    )
    harness.remote.commit(
        "external: pr-480",
        {RELATIVE_PATH: harness.remote.files[RELATIVE_PATH], other_path: dump_yaml_to_string(other_v2)},
    )
    harness.connector.tree = dict(harness.remote.files)
    harness.connector.local_head = harness.remote.head  # disk == remote, cache still behind

    assert "pr-480" not in [d["name"] for d in harness.store.get("other").data["deployments"]]

    # disk == remote, so the old fast path would return here. The cache-head check must
    # still notice the cache is behind and reload.
    await harness.store.reconcile()

    assert "pr-480" in [d["name"] for d in harness.store.get("other").data["deployments"]], (
        "reconcile must repair a cache that lags the disk, even when disk == remote"
    )


async def test_reconcile_logs_error_only_for_drift_not_for_external_pulls(harness: StoreHarness, caplog) -> None:
    """Drift is an unknown path and must be loud; an external pull is routine and must not be.

    The discriminator is whether the disk had to move. If reconcile reloads changed
    data while the disk already matched the remote, no external commit explains it --
    a path advanced the working copy without the cache, which should not happen.
    """
    import logging

    other_path = "projects/other.yaml"
    harness.remote.commit(
        "add other",
        {RELATIVE_PATH: dump_yaml_to_string(_project()), other_path: dump_yaml_to_string(_project(name="other"))},
    )
    harness.connector.tree = dict(harness.remote.files)
    harness.connector.base_ref = harness.remote.head
    harness.connector.local_head = harness.remote.head
    await harness.store.bootstrap()

    # (1) External pull: remote ahead of disk. Routine -- INFO, never ERROR.
    other_v2 = _project(
        name="other",
        deployments=[{"name": "ext", "cluster": "odcn-production", "namespace": "other", "components": []}],
    )
    harness.remote.commit(
        "external", {RELATIVE_PATH: harness.remote.files[RELATIVE_PATH], other_path: dump_yaml_to_string(other_v2)}
    )
    # disk still behind remote (local_head not advanced) -> reset will move it.
    with caplog.at_level(logging.INFO, logger="opi.services.project_store"):
        await harness.store.reconcile()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "an external pull is not an error"
    assert "ext" in [d["name"] for d in harness.store.get("other").data["deployments"]]

    # (2) Drift: disk already at remote, but the cache is stale. This is the anomaly.
    caplog.clear()
    other_v3 = _project(
        name="other",
        deployments=[{"name": "drifted", "cluster": "odcn-production", "namespace": "other", "components": []}],
    )
    harness.remote.commit(
        "silent", {RELATIVE_PATH: harness.remote.files[RELATIVE_PATH], other_path: dump_yaml_to_string(other_v3)}
    )
    harness.connector.tree = dict(harness.remote.files)
    harness.connector.local_head = harness.remote.head  # disk == remote, cache behind

    with caplog.at_level(logging.INFO, logger="opi.services.project_store"):
        await harness.store.reconcile()

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "drift with disk == remote must be logged as an error"
    assert "drifted from git" in errors[0]
    assert "drifted" in [d["name"] for d in harness.store.get("other").data["deployments"]]


def _add_deployment_result(name: str) -> dict[str, Any]:
    """demo with one deployment -- a plain dict for save(), not a change function."""
    return _project(deployments=[{"name": name, "cluster": "odcn-production", "namespace": "demo", "components": []}])


async def test_persist_reports_a_write_that_changed_nothing(harness: StoreHarness, caplog) -> None:
    """Saving identical content logs 'no change' and produces no commit.

    Callers save unconditionally -- process_project_from_git cannot know whether one
    of the resource managers mutated project_data in memory -- so a save whose result
    matches what is already committed is normal, not a bug. It must still be readable
    as such, and it must not create an empty commit in zad-projects.
    """
    import logging

    # Exactly what the remote was seeded with, so the dump is byte-identical.
    with caplog.at_level(logging.INFO, logger="opi.services.project_store"):
        await harness.store.save(PROJECT_NAME, _project(), message="nothing to do", actor="tester")

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("store-persist") and "no change" in m for m in messages), messages
    assert not any(m.startswith("store-push") for m in messages), messages
