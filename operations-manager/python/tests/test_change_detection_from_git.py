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
from opi.services.schema_migration import LATEST_SCHEMA_VERSION
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
    """A brand new file has no baseline, so *everything* must be reported as new.

    This is what drives first-time provisioning: with no previous version the
    diff is synthesised as one "the whole document was added" entry. Reporting
    an empty change set here would silently skip provisioning a new project.
    """
    repo = _repo_with_history(tmp_path, [(_project(["publish-on-web"]), "initial")])

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert analysis["previous_yaml"] is None
    assert analysis["current_yaml"]["services"] == ["publish-on-web"]

    assert analysis["diff"]["dictionary_item_added"]["root"] is analysis["current_yaml"], (
        "a file without a previous version must diff as 'the whole document is new'"
    )
    assert analysis["changes"]["added"], "first version reported no additions, so nothing would be provisioned"
    assert analysis["changes"]["added"][""] == analysis["current_yaml"]


async def test_removed_top_level_key_is_detected(tmp_path: Path) -> None:
    """Removing a whole key, not a list element.

    DeepDiff reports a dropped mapping key as ``dictionary_item_removed``, which
    is a different branch than the ``iterable_item_removed`` a removed service
    produces. Both have to land in ``changes["deleted"]``.
    """
    with_description = _project(["publish-on-web"])
    without_description = _project(["publish-on-web"])
    del without_description["description"]

    repo = _repo_with_history(
        tmp_path,
        [(with_description, "initial"), (without_description, "drop description")],
    )

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert "description" in analysis["changes"]["deleted"], "removal of a top-level key was not reported as a deletion"


async def test_baseline_is_the_immediately_preceding_version_not_the_oldest(tmp_path: Path) -> None:
    """With three versions of the same file, the baseline must be version two.

    One file-touching commit back and the oldest commit are the same thing when
    a file only ever has two versions, so shallow history cannot tell a correct
    implementation from one that always walks to the root commit.
    """
    repo = _repo_with_history(
        tmp_path,
        [
            (_project(["keycloak", "publish-on-web"], description="v1"), "demo: v1"),
            (_project(["publish-on-web"], description="v2"), "demo: v2"),
            (_project(["publish-on-web"], description="v3"), "demo: v3"),
        ],
    )

    analysis = await ProjectFileHandler().analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert analysis["current_yaml"]["description"] == "v3"
    assert analysis["previous_yaml"] is not None
    assert analysis["previous_yaml"]["description"] == "v2", "baseline was not the immediately preceding version"
    assert "keycloak" not in analysis["previous_yaml"]["services"], "baseline came from the oldest commit"


async def test_old_schema_file_is_migrated_and_flagged(tmp_path: Path) -> None:
    """A committed v1 file must come back migrated, with ``was_migrated`` set.

    The caller uses ``was_migrated`` to decide whether to write an auto-migration
    commit back to the projects repo, and the rest of change detection assumes it
    is looking at latest-schema data. Handing the raw v1 document straight through
    would both skip that commit and diff v1 shapes against v2 ones.
    """
    v1_project = {
        "name": "demo",
        "description": "demo",
        "components": [{"name": "web", "type": "single", "uses-services": ["publish-on-web"]}],
    }
    repo = _repo_with_history(tmp_path, [(v1_project, "initial v1 file")])

    handler = ProjectFileHandler()
    analysis = await handler.analyze_project_changes(_connector_on(repo), RELATIVE_PATH)

    assert handler.was_migrated, "migration of a v1 file was not flagged"

    component = analysis["current_yaml"]["components"][0]
    assert "uses-services" not in component, "v1 key survived, so the data was not migrated"
    assert component["services"] == ["publish-on-web"]
    assert analysis["current_yaml"]["schema-version"] == LATEST_SCHEMA_VERSION
