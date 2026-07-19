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
        patch("opi.services.project_store.save_yaml_to_path"),
    ):
        await pm.save_and_commit_project(project_data, "Add component")

    refreshed = get_project_service().get_project("cache-refresh-demo")
    assert refreshed is not None
    assert refreshed.data["name"] == "cache-refresh-demo"
    assert any(d.get("name") == "prod" for d in refreshed.data.get("deployments", []))
