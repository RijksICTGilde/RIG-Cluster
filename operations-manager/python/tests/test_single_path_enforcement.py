"""Single-path consolidation guard + regression tests.

The project file has exactly one validated save path:
``ProjectManager.save_and_commit_project`` (schema + structural validation,
canonical dumper, commit + push, cache refresh). No mutation handler in the
API/web/core/service layers may persist project YAML to git directly -- doing so
re-opens the validation-bypass gap this consolidation closed (an API call could
commit a malformed or structurally broken project file to ``zad-projects``).

These tests fail closed if a future change reintroduces a direct project-file
commit, and pin the validate-before-commit + cache-refresh behaviour of the
central save.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.project_store import GitProjectStore

# Directories whose handlers must route every project-file write through
# ProjectManager.save_and_commit_project (the central validated save).
_PYTHON_ROOT = Path(__file__).resolve().parents[1]
_SCANNED_DIRS = ["opi/api", "opi/web", "opi/core", "opi/services"]

# Substrings that indicate a project-YAML write that skips the central save.
_FORBIDDEN_PATTERNS = (
    "do_commit_and_push=True",  # raw create_or_update_file commit, no validation
    "commit_project_yaml(",  # removed legacy helper, must never come back
    "save_project_file(",  # low-level writer; only the central save may call it
)

# Files allowed to still contain a forbidden pattern, with the reason. These are
# NOT live mutation paths. Keep this list empty of live handlers.
_ALLOWLIST: dict[str, str] = {}

# Direct access to the zad-projects repo. Every project-file read/write must go
# through the ProjectStore, which owns the single warm working copy: a new
# per-request clone here reintroduces the two-clones-per-edit cost and, worse,
# writes outside the store's lock (no serialization, no validated final state).
_FORBIDDEN_GIT_ACCESS = "create_git_connector_for_project_files("

# The factory is not the only way in: a module can build the same connector by
# hand with GitConnector(repo_url=settings.GIT_PROJECTS_SERVER_URL, ...). That is
# how the create-project handlers ended up reading a clone taken BEFORE the store
# wrote and pushed the new file ("Project file not found at HEAD"), while every
# test stayed green -- the scan below only looked for the factory call.
#
# Scoped to the projects repo on purpose. Connectors for zad-deployments and
# zad-argo-user-applications are built by hand all over ProjectManager and are
# explicitly outside the store's scope.
_PROJECTS_REPO_SETTING = "GIT_PROJECTS_SERVER_URL"

_GIT_ACCESS_ALLOWLIST: dict[str, str] = {
    # The store IS the owner of the projects-repo connector.
    "opi/services/project_store.py": "owns the warm working copy",
}


def _scan_for_direct_project_commits() -> list[str]:
    violations: list[str] = []
    for rel_dir in _SCANNED_DIRS:
        for py_file in (_PYTHON_ROOT / rel_dir).rglob("*.py"):
            rel_path = py_file.relative_to(_PYTHON_ROOT).as_posix()
            if rel_path in _ALLOWLIST:
                continue
            lines = py_file.read_text(encoding="utf-8").splitlines()
            violations.extend(
                f"{rel_path}:{lineno}: {pattern.rstrip('(')} -> {line.strip()}"
                for lineno, line in enumerate(lines, 1)
                for pattern in _FORBIDDEN_PATTERNS
                if pattern in line
            )
    return violations


def test_no_direct_project_file_commits_outside_central_save() -> None:
    """No api/web/core/service handler may commit project YAML directly.

    Every project-file mutation must go through ProjectManager.save_and_commit_project
    so schema + structural validation runs before the commit. A failure here means a
    new direct commit was introduced (the validation-bypass gap reopened); route it
    through save_and_commit_project instead.
    """
    violations = _scan_for_direct_project_commits()
    assert not violations, "Direct project-file commits found (must use save_and_commit_project):\n" + "\n".join(
        violations
    )


def _scan_for_direct_projects_repo_access() -> list[str]:
    violations: list[str] = []
    for rel_dir in _SCANNED_DIRS:
        for py_file in (_PYTHON_ROOT / rel_dir).rglob("*.py"):
            rel_path = py_file.relative_to(_PYTHON_ROOT).as_posix()
            if rel_path in _GIT_ACCESS_ALLOWLIST:
                continue
            source = py_file.read_text(encoding="utf-8")
            lines = source.splitlines()
            violations.extend(
                f"{rel_path}:{lineno}: {line.strip()}"
                for lineno, line in enumerate(lines, 1)
                if _FORBIDDEN_GIT_ACCESS in line
            )
            violations.extend(_scan_for_handbuilt_projects_connector(rel_path, source))
    return violations


def _scan_for_handbuilt_projects_connector(rel_path: str, source: str) -> list[str]:
    """Find ``GitConnector(...)`` calls that point at the zad-projects repo.

    Matched on the constructor plus its argument list rather than on a helper name,
    because the way around the store was never the helper: it was building the same
    connector inline. Only the projects repo counts -- deployments and argo
    connectors are built this way legitimately.
    """
    violations: list[str] = []
    for match in re.finditer(r"GitConnector\(", source):
        depth, end = 0, len(source)
        for pos in range(match.end() - 1, len(source)):
            if source[pos] == "(":
                depth += 1
            elif source[pos] == ")":
                depth -= 1
                if depth == 0:
                    end = pos
                    break
        if _PROJECTS_REPO_SETTING in source[match.end() : end]:
            lineno = source.count("\n", 0, match.start()) + 1
            violations.append(f"{rel_path}:{lineno}: GitConnector(...) built against the projects repo")
    return violations


_STORE_CONNECTOR_SOURCES = ("get_connector()", "get_git_connector_for_project_files()", "get_project_data_from_git(")

# Assignment of a name from one of the store sources, including tuple unpacking
# (get_project_data_from_git returns ``data, filename, connector``).
_STORE_ASSIGN = re.compile(
    r"^\s*(?P<lhs>[\w.,\s]+?)\s*=\s*await\s+.*(?:{})".format("|".join(re.escape(s) for s in _STORE_CONNECTOR_SOURCES))
)
# A name the module constructs itself, and therefore genuinely owns.
_OWN_ASSIGN = re.compile(r"^\s*(\w+)\s*=\s*(?:await\s+)?GitConnector\(")


def _scan_for_warm_connector_closes() -> list[str]:
    """Find code that closes a connector obtained from the ProjectStore.

    GitConnector.close() rmtree's the working directory unconditionally, so closing the
    store's shared warm copy deletes it out from under every other project-file operation
    in the process -- unrecoverable short of a restart, because the store caches the
    connector and ensure_repo_cloned() short-circuits on its _repo_cloned flag.

    Scoped per variable name: a name the module builds itself with ``GitConnector(...)``
    is genuinely owned and may be closed, so it never counts as a violation.
    """
    violations: list[str] = []
    for rel_dir in [*_SCANNED_DIRS, "opi/manager"]:
        for py_file in (_PYTHON_ROOT / rel_dir).rglob("*.py"):
            rel_path = py_file.relative_to(_PYTHON_ROOT).as_posix()
            if rel_path == "opi/services/project_store.py":
                continue
            lines = [
                line for line in py_file.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("#")
            ]

            # Only plain local names. An attribute (self.__git_connector_for_project_files)
            # is the sanctioned way to hold a connector that may or may not be owned --
            # ProjectManager guards its close with an explicit ownership flag -- so those
            # are not violations.
            from_store = {
                m.group("lhs").split(",")[-1].strip()
                for line in lines
                if (m := _STORE_ASSIGN.match(line)) and "." not in m.group("lhs")
            }
            self_owned = {m.group(1) for line in lines if (m := _OWN_ASSIGN.match(line))}
            borrowed = {name for name in from_store - self_owned if name.isidentifier()}
            if not borrowed:
                continue

            # Word-boundary match: `self.__git_connector_for_project_files.close()` must not
            # be mistaken for the local `git_connector_for_project_files.close()`.
            close_patterns = [re.compile(rf"(?<![\w.]){re.escape(name)}\.close\(\)") for name in borrowed]
            violations.extend(
                f"{rel_path}:{lineno}: {line.strip()}"
                for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), 1)
                if not line.strip().startswith("#") and any(p.search(line) for p in close_patterns)
            )
    return violations


def test_no_one_closes_the_stores_warm_connector() -> None:
    """Closing the store's warm connector deletes the shared working copy.

    This is not hypothetical: oom_watcher did exactly this after the connector's
    ownership contract was inverted (get_project_data_from_git went from handing out a
    per-call clone the caller owned, to handing out the shared warm copy), and the
    documented "never close the warm connector" invariant did not stop it. A comment
    cannot enforce an invariant; this test can.

    If you genuinely own a connector you created yourself, close it via a differently
    named variable, or close the owning ProjectManager (which respects the not-owned flag).
    """
    violations = _scan_for_warm_connector_closes()
    assert not violations, (
        "Code closing a ProjectStore-owned git connector (this rmtree's the shared warm "
        "working copy):\n" + "\n".join(violations)
    )


def _scan_for_direct_cache_access() -> list[str]:
    """Find code reaching the project cache without going through ProjectStore.

    ProjectService is the in-memory project cache. Reading or writing it directly is
    a second door into project files: it skips the store's freshness handling, and a
    direct write can leave the cache disagreeing with what is actually in git.
    """
    violations: list[str] = []
    for rel_dir in [*_SCANNED_DIRS, "opi/manager", "opi/web", "opi/handlers", "opi/middleware", "opi/forms"]:
        directory = _PYTHON_ROOT / rel_dir
        if not directory.exists():
            continue
        for py_file in directory.rglob("*.py"):
            rel_path = py_file.relative_to(_PYTHON_ROOT).as_posix()
            if rel_path in ("opi/services/project_store.py", "opi/services/project_service.py"):
                continue
            violations.extend(
                f"{rel_path}:{lineno}: {line.strip()}"
                for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), 1)
                if "get_project_service()" in line and not line.strip().startswith("#")
            )
    return violations


def test_project_cache_is_reached_only_through_the_store() -> None:
    """ProjectStore is the only door to project files, for reads as well as writes.

    Reads used to go straight to ProjectService in 64 places, and four of those wrote
    to the cache directly -- which is how the cache and git drift apart. Everything now
    goes through ProjectStore.get/get_all/get_by_api_key, so there is one place that
    decides what a project file says.

    Need project data? Use get_project_store(). Need to know whether a user may touch a
    project? opi.services.project_authorization. Need the platform-admin allowlist?
    UserService.
    """
    violations = _scan_for_direct_cache_access()
    assert not violations, "Direct project-cache access found (must go through ProjectStore):\n" + "\n".join(violations)


def test_no_direct_projects_repo_clones_outside_the_store() -> None:
    """Project-file git access belongs to the ProjectStore alone.

    A new direct clone of zad-projects means a write that bypasses the store's
    per-repo lock and its validate-on-final-state guarantee, and re-adds the
    per-request clone cost the store exists to remove. Route it through
    ProjectStore (store.get/mutate/save, or store.get_connector() when a raw
    connector is genuinely needed) instead of adding an entry to the allowlist.
    """
    violations = _scan_for_direct_projects_repo_access()
    assert not violations, "Direct zad-projects clones found (must go through ProjectStore):\n" + "\n".join(violations)


def test_no_one_injects_a_projects_connector_into_project_manager() -> None:
    """ProjectManager must resolve the projects connector itself, from the store.

    Injecting one wins over the store: get_git_connector_for_project_files() only
    falls back to the warm copy when nothing was passed in. Both create-project
    handlers used to inject a connector cloned before their own write, so the
    processing step that followed read a HEAD without the new file and every
    create failed with "Project file not found at HEAD".
    """
    violations: list[str] = []
    for rel_dir in [*_SCANNED_DIRS, "opi/manager", "opi/handlers"]:
        for py_file in (_PYTHON_ROOT / rel_dir).rglob("*.py"):
            rel_path = py_file.relative_to(_PYTHON_ROOT).as_posix()
            if rel_path == "opi/manager/project_manager.py":
                continue  # declares the parameter and holds it as an attribute
            violations.extend(
                f"{rel_path}:{lineno}: {line.strip()}"
                for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), 1)
                if "git_connector_for_project_files=" in line and not line.strip().startswith("#")
            )
    assert not violations, (
        "A projects-repo connector is injected into ProjectManager (it must come from the store):\n"
        + "\n".join(violations)
    )


def test_no_project_file_is_read_from_a_filesystem_path() -> None:
    """Project files are read from git objects, never off the warm working copy.

    The warm copy is shared, and ProjectStore.reconcile() does `reset --hard` plus
    `clean -fd` on it under a lock that a reader on a plain filesystem path does not
    hold -- so such a read can observe a half-rewritten or missing file. Both readers
    that used to do this (ProjectFileHandler.read_project_file and
    ProjectManager.get_project_full_file_path) were removed with their callers.

    Use store.get() / store.read_path() / store.read_at(), or
    ProjectFileHandler.read_committed_project_file(), all of which read committed
    objects and cannot tear.
    """
    forbidden = ("read_project_file(", "get_project_full_file_path(")
    violations: list[str] = []

    for path in sorted((_PYTHON_ROOT / "opi").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("#", '"')):
                continue
            violations.extend(
                f"{path.relative_to(_PYTHON_ROOT)}:{lineno}: {stripped[:90]}"
                for pattern in forbidden
                if pattern in stripped and "def " not in stripped
            )

    assert not violations, "Project file read from a filesystem path (must read committed objects):\n" + "\n".join(
        violations
    )


@pytest.mark.asyncio
async def test_central_save_refreshes_cache_with_new_state(tmp_path) -> None:
    """After a successful save, the read-only cache reflects the committed state."""
    from opi.services.project_service import get_project_service

    pm = ProjectManager.__new__(ProjectManager)
    pm._project_file_relative_path = "projects/cache-refresh-demo.yaml"

    project_data = {
        "name": "cache-refresh-demo",
        "description": "x",
        "clusters": ["local"],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "config": {"api-key": "base64+age:Zm9vYmFy"},
        "repositories": [{"name": "r", "url": "ssh://git@h:2222/srv/git/x.git", "branch": "main", "path": "infra"}],
        "components": [{"name": "frontend", "type": "deployment"}],
        "deployments": [
            {
                "name": "prod",
                "cluster": "local",
                "namespace": "cache-refresh-demo",
                "repository": "r",
                "components": [{"reference": "frontend", "image": "nginx:latest"}],
            }
        ],
    }

    # The persist path runs inside GitProjectStore now, so drive a real store with
    # a fake git connector; the cache write-through under test is the store's.
    git = AsyncMock()
    git.show_file_at = AsyncMock(return_value=None)
    git.get_local_commit_hash = AsyncMock(return_value="deadbeef")

    store = GitProjectStore(working_dir=str(tmp_path))

    async def _get_connector():
        return git

    store.get_connector = _get_connector

    with (
        patch("opi.services.project_store.validate_project_structure", new=AsyncMock()),
        patch("opi.manager.project_manager.get_project_store", return_value=store),
    ):
        await pm.save_and_commit_project(project_data, "Add component")

    refreshed = get_project_service().get_project("cache-refresh-demo")
    assert refreshed is not None
    assert refreshed.data["name"] == "cache-refresh-demo"
    assert any(d.get("name") == "prod" for d in refreshed.data.get("deployments", []))
