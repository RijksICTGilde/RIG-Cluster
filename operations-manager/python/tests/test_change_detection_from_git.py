"""Change detection must keep working now that it reads from git objects.

``analyze_project_changes`` drives the reconcile logic that notices, for example,
that a service was removed from a project file and that its resources should be
torn down. It used to read the current version off the shared warm working copy,
which races ProjectStore.reconcile()'s `reset --hard`; it now reads the committed
object with `git show`.

That path had no test at all, so this pins the behaviour that matters: current vs
previous is still resolved file-scoped, and removals, additions and modifications
are still reported.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from opi.connectors.git import GitConnector
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.utils.yaml_util import dump_yaml_to_string

if TYPE_CHECKING:
    from pathlib import Path

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

RELATIVE_PATH = "projects/demo.yaml"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout.strip()


def _project(services: list[str], description: str = "demo") -> dict:
    return {
        "schema-version": 2,
        "name": "demo",
        "description": description,
        "services": services,
        "components": [{"name": "web", "type": "single"}],
    }


def _write_and_commit(repo: Path, data: dict, message: str) -> None:
    (repo / RELATIVE_PATH).write_text(dump_yaml_to_string(data))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _repo_with_history(tmp_path: Path, versions: list[tuple[dict, str]]) -> Path:
    repo = tmp_path / "repo"
    (repo / "projects").mkdir(parents=True)
    _git(repo, "init", "-q")
    for data, message in versions:
        _write_and_commit(repo, data, message)
    return repo


def _connector_on(repo: Path) -> GitConnector:
    conn = GitConnector(repo_url="https://example.invalid/repo.git", working_dir=str(repo), full_history=True)
    conn._repo_cloned = True
    conn._fetched_in_session = True
    return conn


async def test_removed_service_is_detected(tmp_path: Path) -> None:
    """The case the reconcile logic exists for: a service disappeared."""
    repo = _repo_with_history(
        tmp_path,
        [
            (_project(["keycloak", "publish-on-web"]), "initial"),
            (_project(["publish-on-web"]), "remove keycloak"),
        ],
    )

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert analysis["current_yaml"]["services"] == ["publish-on-web"]
    assert analysis["previous_yaml"] is not None
    assert "keycloak" in analysis["previous_yaml"]["services"]
    assert analysis["changes"]["deleted"], "removal of a service was not reported"


async def test_added_service_is_detected(tmp_path: Path) -> None:
    repo = _repo_with_history(
        tmp_path,
        [
            (_project(["publish-on-web"]), "initial"),
            (_project(["publish-on-web", "keycloak"]), "add keycloak"),
        ],
    )

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert analysis["changes"]["added"], "addition of a service was not reported"


async def test_modified_field_is_detected(tmp_path: Path) -> None:
    repo = _repo_with_history(
        tmp_path,
        [
            (_project(["publish-on-web"], description="oud"), "initial"),
            (_project(["publish-on-web"], description="nieuw"), "change description"),
        ],
    )

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert analysis["changes"]["changed"], "modification was not reported"


async def test_baseline_ignores_commits_touching_other_projects(tmp_path: Path) -> None:
    """The projects repo is shared, so HEAD~1 is usually someone else's commit.

    The previous version must be resolved file-scoped, or the diff compares this
    project against an unrelated change and reports nonsense.
    """
    repo = _repo_with_history(
        tmp_path,
        [
            (_project(["keycloak", "publish-on-web"]), "demo: initial"),
            (_project(["publish-on-web"]), "demo: remove keycloak"),
        ],
    )

    # An unrelated project changes after ours -- this is what HEAD~1 would point at.
    (repo / "projects" / "ander.yaml").write_text("name: ander\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ander: unrelated commit")

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert analysis["previous_yaml"] is not None
    assert "keycloak" in analysis["previous_yaml"]["services"], (
        "baseline came from the unrelated commit instead of this file's history"
    )


async def test_first_version_has_no_previous(tmp_path: Path) -> None:
    repo = _repo_with_history(tmp_path, [(_project(["publish-on-web"]), "initial")])

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert analysis["previous_yaml"] is None
    assert analysis["current_yaml"]["services"] == ["publish-on-web"]
