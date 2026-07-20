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

_GIT_ACCESS_ALLOWLIST: dict[str, str] = {
    # The store IS the owner of the projects-repo connector.
    "opi/services/project_store.py": "owns the warm working copy",
    # Not yet swept: three read-only restore paths still open their own clone.
    # They are correct but pay for a clone; routing them onto the store is
    # tracked as follow-up work and needs the surrounding `async with` blocks
    # restructured (the warm connector must never be closed).
    "opi/api/restore_router.py": "pending sweep onto the store (read-only restore paths)",
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
            lines = py_file.read_text(encoding="utf-8").splitlines()
            violations.extend(
                f"{rel_path}:{lineno}: {line.strip()}"
                for lineno, line in enumerate(lines, 1)
                if _FORBIDDEN_GIT_ACCESS in line
            )
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
