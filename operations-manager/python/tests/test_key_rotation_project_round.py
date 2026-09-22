"""Tests for scripts/project_rotation.py: the round over a directory of project files.

The round is where the rotation either holds together or quietly falls apart, so these tests
measure the properties that a "it ran" check would miss:

* one unreadable file does not abort the round -- the projects after it are still done;
* that file is not half written, and it is reported as a real problem rather than a skip;
* one commit per project, so a single bad file can be reverted on its own;
* a second round does nothing and says so;
* a PAT replacement changes the hash at the password fields and NOT at the project key.

The keys come from ``generate_sops_key_pair`` per test. There is no fixed key in this file.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from opi.utils.age import BASE64_AGE_PREFIX, encrypt_age_content
from opi.utils.sops import generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_path

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from key_rotation import (  # noqa: E402
    PROJECT_FIELD_PRIVATE_KEY,
    decrypt_field,
    opens_with,
)
from project_rotation import broken, find_repo_root, run_round  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="requires the age binary")

PROJECT_TEMPLATE = """\
schema-version: 2
name: {name}
display-name: {name}
description: Project for the rotation test
users:
  - email: someone@rijksoverheid.nl
    role: admin
clusters:
  - odcn-production
repositories:
  - name: main-repo
    url: https://github.com/example/app.git
    username: git
    password: {repo_password}
    branch: main
    path: .
components:
  - name: component-1
    type: single
    ports:
      inbound: [8000]
      outbound: [80, 443]
deployments:
  - name: deployment-1
    cluster: odcn-production
    namespace: {name}
    repository: main-repo
    components:
      - reference: component-1
        image: ghcr.io/example/app:1
config:
  age-public-key: {project_public}
  age-private-key: |-
{project_private}
"""


def _indent(value: str, spaces: int = 4) -> str:
    return "\n".join(" " * spaces + line for line in value.splitlines())


async def _write_project(directory: Path, name: str, platform_public: str) -> Path:
    """One project file shaped like the real ones: block-scalar key, one-line repo password."""
    project_private, project_public = generate_sops_key_pair()
    block = await encrypt_age_content("ghp_repository_token", platform_public)
    path = directory / f"{name}.yaml"
    path.write_text(
        PROJECT_TEMPLATE.format(
            name=name,
            repo_password=f"{BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}",
            project_public=project_public,
            project_private=_indent(await encrypt_age_content(project_private, platform_public)),
        )
    )
    return path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout


@pytest.fixture
def projects_repo(tmp_path: Path) -> Path:
    """A real git working tree, because the round commits per project."""
    repo = tmp_path / "zad-projects"
    (repo / "projects").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


@pytest.mark.asyncio
async def test_a_round_converts_every_file_and_commits_one_per_project(
    projects_repo: Path,
) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    for name in ("een", "twee", "drie"):
        await _write_project(directory, name, old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    before_commits = len(_git(projects_repo, "log", "--oneline").splitlines())

    result = await run_round(directory, old_private, new_private, dry_run=False)

    assert len(result.converted) == 3
    assert result.fields == 6
    assert result.committed == ["drie.yaml", "een.yaml", "twee.yaml"]
    after_commits = len(_git(projects_repo, "log", "--oneline").splitlines())
    assert after_commits == before_commits + 3
    assert _git(projects_repo, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_a_round_leaves_the_content_alone(projects_repo: Path) -> None:
    """Both platform fields move to the new key, and the old key opens neither."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    result = await run_round(directory, old_private, new_private, dry_run=False)

    assert not result.fingerprint_before.compare(result.fingerprint_after)
    data = load_yaml_from_path(str(path))
    assert await decrypt_field(data["repositories"][0]["password"], new_private) == "ghp_repository_token"
    assert not await opens_with(data["config"]["age-private-key"], old_private)
    assert not await opens_with(data["repositories"][0]["password"], old_private)


@pytest.mark.asyncio
async def test_one_unreadable_file_does_not_stop_the_round(projects_repo: Path) -> None:
    """The failure mode this guards: stopping halfway leaves half the projects on each key."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    for name in ("aaa", "bbb", "ccc"):
        await _write_project(directory, name, old_public)
    broken_path = directory / "bbb.yaml"
    broken_text = broken_path.read_text()
    corrupted = broken_text.replace(broken_text.split("password: ")[1].split("\n")[0], f"{BASE64_AGE_PREFIX}QUJDREVG")
    broken_path.write_text(corrupted)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    result = await run_round(directory, old_private, new_private, dry_run=False)

    assert [report.path.name for report in result.converted] == ["aaa.yaml", "ccc.yaml"]
    assert [report.path.name for report in result.skipped] == ["bbb.yaml"]
    assert "opens with neither key" in (result.skipped[0].skipped or "")
    # Not half written: the broken file is exactly as it was.
    assert broken_path.read_text() == corrupted
    assert result.committed == ["aaa.yaml", "ccc.yaml"]


@pytest.mark.asyncio
async def test_an_unreadable_file_counts_as_a_real_problem(projects_repo: Path) -> None:
    """A silent skip would read as success; the caller's exit code hangs off this."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    path = await _write_project(directory, "kapot", old_public)
    text = path.read_text()
    path.write_text(text.replace(text.split("password: ")[1].split("\n")[0], f"{BASE64_AGE_PREFIX}QUJDREVG"))

    result = await run_round(directory, old_private, new_private, dry_run=False)

    assert len(broken(result)) == 1


@pytest.mark.asyncio
async def test_an_already_converted_file_is_not_a_problem(projects_repo: Path) -> None:
    """The other side of the same judgement: a second round must exit clean."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    await run_round(directory, old_private, new_private, dry_run=False)
    second = await run_round(directory, old_private, new_private, dry_run=False)

    assert second.converted == []
    assert len(second.skipped) == 1
    assert "already converted" in (second.skipped[0].skipped or "")
    assert broken(second) == []
    assert second.committed == []


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing_and_commits_nothing(projects_repo: Path) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    before = path.read_text()

    result = await run_round(directory, old_private, new_private, dry_run=True)

    assert result.fields == 2
    assert result.committed == []
    assert path.read_text() == before
    assert _git(projects_repo, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_a_pat_round_replaces_the_password_and_keeps_the_project_key(
    projects_repo: Path,
) -> None:
    """One loop, two entries: the hash differs at the password and matches at the key."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    result = await run_round(directory, old_private, new_private, new_pat="ghp_new", dry_run=False)

    assert result.replaced_fields == [f"{path}#repositories[0].password"]
    # The announced difference is accepted; an unannounced one would not be.
    assert not result.fingerprint_before.compare(result.fingerprint_after, replaced=result.replaced_fields)
    assert result.fingerprint_before.compare(result.fingerprint_after) == [
        f"content changed: {path}#repositories[0].password"
    ]
    data = load_yaml_from_path(str(path))
    assert await decrypt_field(data["repositories"][0]["password"], new_private) == "ghp_new"
    key_field = f"{path}#{PROJECT_FIELD_PRIVATE_KEY}"
    assert result.fingerprint_before.fields[key_field] == result.fingerprint_after.fields[key_field]


@pytest.mark.asyncio
async def test_no_commit_still_writes_the_files(projects_repo: Path) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    result = await run_round(directory, old_private, new_private, dry_run=False, commit=False)

    assert result.converted
    assert result.committed == []
    assert _git(projects_repo, "status", "--porcelain").strip().endswith("projects/een.yaml")
    assert await opens_with(load_yaml_from_path(str(path))["config"]["age-private-key"], new_private)


def test_find_repo_root_returns_none_outside_a_repository(tmp_path: Path) -> None:
    """Without this the round would try to commit in a plain directory and raise."""
    outside = tmp_path / "loose"
    outside.mkdir()
    assert find_repo_root(outside) is None
