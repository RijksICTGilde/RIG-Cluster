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
from unittest.mock import patch

import pytest
from opi.utils.age import BASE64_AGE_PREFIX, encrypt_age_content
from opi.utils.sops import generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_path

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import sops_rotation as final_check_tool  # noqa: E402
from key_rotation import (  # noqa: E402
    PROJECT_FIELD_PRIVATE_KEY,
    Fingerprint,
    MissingKey,
    ProjectRound,
    decrypt_field,
    opens_with,
)
from project_rotation import (  # noqa: E402
    RoundResult,
    broken,
    find_repo_root,
    main_replace_pat,
    main_rotate_keys,
    read_pat,
    run_round,
)

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
{repositories}
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


async def _repositories(platform_public: str, passwords: tuple[str | None, ...]) -> str:
    """The ``repositories:`` block, one entry per element of ``passwords``.

    ``None`` is a repository that carries no password at all. That is not a contrived shape:
    two of the three repositories in this repo's own ``projects/simple-example.yaml`` look
    exactly like that, and a project made only of them has nothing for the PAT round to do.
    """
    lines: list[str] = []
    for index, password in enumerate(passwords):
        lines.append(f"  - name: {'main-repo' if index == 0 else f'repo-{index + 1}'}")
        lines.append("    url: https://github.com/example/app.git")
        if password is not None:
            block = await encrypt_age_content(password, platform_public)
            lines.append("    username: git")
            lines.append(f"    password: {BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}")
        lines.append("    branch: main")
        lines.append("    path: .")
    return "\n".join(lines)


async def _write_project(
    directory: Path,
    name: str,
    platform_public: str,
    *,
    passwords: tuple[str | None, ...] = ("ghp_repository_token",),
) -> Path:
    """One project file shaped like the real ones: block-scalar key, one-line repo passwords."""
    project_private, project_public = generate_sops_key_pair()
    path = directory / f"{name}.yaml"
    path.write_text(
        PROJECT_TEMPLATE.format(
            name=name,
            repositories=await _repositories(platform_public, passwords),
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
async def test_a_yaml_without_encrypted_platform_fields_is_no_problem_in_either_round(
    projects_repo: Path,
) -> None:
    """The round globs ``*.yaml``, so whatever else sits in that directory reaches it too.

    Judged as a real problem it would make every round exit 1 over a file that has nothing to
    convert, in both the key round and the PAT round.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    (directory / "leeg.yaml").write_text("schema-version: 2\nname: leeg\n")

    key_round = await run_round(directory, old_private, new_private, dry_run=False)
    pat_round = await run_round(directory, old_private, new_private, new_pat="ghp_new", dry_run=False)

    assert [report.skipped for report in key_round.skipped] == ["no encrypted platform fields in this file"]
    assert broken(key_round) == []
    assert broken(pat_round, pat_round=True) == []


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


# ---------------------------------------------------------------------------
# the two entry points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_key_entry_point_refuses_a_directory_that_is_not_one(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    code = await main_rotate_keys(["--projects", str(tmp_path / "absent")])
    assert code == 2
    assert "not a directory" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_key_entry_point_refuses_the_same_key_twice(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    private, _public = generate_sops_key_pair()
    key_file = tmp_path / "key.txt"
    key_file.write_text(f"{private}\n")
    (tmp_path / "projects").mkdir()

    code = await main_rotate_keys(
        [
            "--ja",
            "--projects",
            str(tmp_path / "projects"),
            "--old-key",
            str(key_file),
            "--new-key",
            str(key_file),
        ]
    )

    assert code == 2
    assert "the same key" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_key_entry_point_dry_run_writes_no_fingerprint(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A dry run must leave nothing behind, the fingerprint file included."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    await _write_project(projects_repo / "projects", "een", old_public)
    fingerprint = tmp_path / "fingerprint.json"

    code = await main_rotate_keys(
        [
            "--ja",
            "--dry-run",
            "--projects",
            str(projects_repo / "projects"),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(fingerprint),
        ]
    )

    assert code == 0
    assert not fingerprint.exists()
    assert "Dry run: nothing was changed." in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_key_entry_point_exits_red_on_an_unreadable_file(projects_repo: Path, tmp_path: Path) -> None:
    """A silent skip would read as success to whatever runs this next."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    path = await _write_project(projects_repo / "projects", "kapot", old_public)
    text = path.read_text()
    path.write_text(text.replace(text.split("password: ")[1].split("\n")[0], f"{BASE64_AGE_PREFIX}QUJDREVG"))

    code = await main_rotate_keys(
        [
            "--ja",
            "--dry-run",
            "--projects",
            str(projects_repo / "projects"),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
        ]
    )

    assert code == 1


@pytest.mark.asyncio
async def test_the_key_entry_point_writes_the_fingerprint_it_will_be_checked_against(
    projects_repo: Path, tmp_path: Path
) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "fingerprint.json"

    code = await main_rotate_keys(
        [
            "--ja",
            "--projects",
            str(projects_repo / "projects"),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(fingerprint),
        ]
    )

    assert code == 0
    written = fingerprint.read_text()
    assert len(Fingerprint.load(fingerprint).fields) == 2
    assert "ghp_repository_token" not in written
    assert "AGE-SECRET-KEY-" not in written


@pytest.mark.asyncio
async def test_the_fingerprint_this_round_writes_is_the_count_the_final_check_expects(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The two tools have to agree on the number, and only running both proves that they do.

    ``rotate-project-keys.py`` records the count and ``rotate-sops-key.py --assert-old-key-dead``
    checks against it, but each half is otherwise tested against a fingerprint written by hand.
    A round that records a different SET of fields than the check walks ends on "count differs",
    which reads as a failed rotation while nothing is wrong with the key. The second half is
    there because a count check that is not running at all also prints no complaint: with one
    field taken out of the record, the same command has to go red.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    for name in ("een", "twee", "drie"):
        await _write_project(directory, name, old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"
    keys = ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]

    round_code = await main_rotate_keys(
        ["--ja", "--projects", str(directory), *keys, "--fingerprint", str(fingerprint)]
    )
    capsys.readouterr()
    with (
        patch.object(final_check_tool, "sops_files_for", return_value=[]),
        patch.object(final_check_tool, "env_paths", return_value=[]),
        patch.object(final_check_tool, "OWN_PROJECTS", tmp_path / "not-a-directory"),
        patch.object(final_check_tool, "DEFAULT_PROJECTS_FINGERPRINT", fingerprint),
    ):
        command = [
            "--ja",
            "--assert-old-key-dead",
            "--projects",
            str(directory),
            *keys,
            "--fingerprint",
            str(tmp_path / "no-fingerprint-for-this-repo.json"),
        ]
        step_8 = await final_check_tool.main(command)
        printed = capsys.readouterr().out
        recorded = Fingerprint.load(fingerprint)
        recorded.fields.popitem()
        recorded.save(fingerprint)
        with_a_field_short = await final_check_tool.main(command)

    assert round_code == 0
    assert len(recorded.fields) == 5
    assert "6 fields checked" in printed
    assert "count differs" not in printed
    assert step_8 == 0
    assert with_a_field_short == 1
    assert "FAIL count differs from the fingerprint: 6 now, 5 before" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_second_key_round_leaves_the_fingerprint_of_the_first_alone(
    projects_repo: Path, tmp_path: Path
) -> None:
    """Running the tool twice is a promise of this tool, and the second run converts nothing.

    Why that matters is in ``save_fingerprint``. Measured before the fix: round one 6 fields,
    round two 0, and step 6 red with the right flag. This goes through the entry point on
    purpose: ``run_round`` sits UNDER the layer that saves.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [
        "--ja",
        "--projects",
        str(projects_repo / "projects"),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(fingerprint),
    ]

    first = await main_rotate_keys(arguments)
    after_the_first_round = fingerprint.read_text()
    second = await main_rotate_keys(arguments)

    assert first == 0
    assert second == 0
    assert len(Fingerprint.load(fingerprint).fields) == 2
    assert fingerprint.read_text() == after_the_first_round


def test_the_pat_is_read_from_a_file(tmp_path: Path) -> None:
    (tmp_path / "pat.txt").write_text("ghp_from_a_file\n")
    assert read_pat(str(tmp_path / "pat.txt")) == "ghp_from_a_file"


def test_an_empty_pat_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pat.txt").write_text("\n")
    with pytest.raises(MissingKey):
        read_pat(str(tmp_path / "pat.txt"))


def test_the_pat_is_asked_for_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    """getpass and not input: a typed PAT must not end up in a terminal scrollback either."""
    asked: list[str] = []

    def fake_getpass(prompt: str) -> str:
        asked.append(prompt)
        return "ghp_typed"

    monkeypatch.setattr("project_rotation.getpass.getpass", fake_getpass)
    assert read_pat(None) == "ghp_typed"
    assert len(asked) == 1
    assert "not echoed" in asked[0]


def test_an_empty_typed_pat_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("project_rotation.getpass.getpass", lambda _prompt: "   ")
    with pytest.raises(MissingKey):
        read_pat(None)


@pytest.mark.asyncio
async def test_the_pat_entry_point_replaces_the_password_and_keeps_the_key(projects_repo: Path, tmp_path: Path) -> None:
    """End to end over the entry point: the announced difference passes the content check."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_brand_new\n")
    path = await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    code = await main_replace_pat(
        [
            "--ja",
            "--projects",
            str(projects_repo / "projects"),
            "--pat-file",
            str(tmp_path / "pat.txt"),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(tmp_path / "pat-fingerprint.json"),
        ]
    )

    assert code == 0
    data = load_yaml_from_path(str(path))
    assert await decrypt_field(data["repositories"][0]["password"], new_private) == "ghp_brand_new"
    assert await opens_with(data["config"]["age-private-key"], new_private)


# ---------------------------------------------------------------------------
# the documented order: the key round first, the PAT round after it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pat_round_still_replaces_after_the_key_round(projects_repo: Path, tmp_path: Path) -> None:
    """The order the plan advises, driven through both entry points in turn.

    Without the worklist looking at the token, the second round hands back "already converted",
    exit 0 and "revoke the old PAT" -- with the old PAT still in the file.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_brand_new\n")
    path = await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    keys = [
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--projects",
        str(projects_repo / "projects"),
        "--ja",
    ]

    key_round = await main_rotate_keys([*keys, "--fingerprint", str(tmp_path / "key-fingerprint.json")])
    pat_round = await main_replace_pat(
        [
            *keys,
            "--pat-file",
            str(tmp_path / "pat.txt"),
            "--fingerprint",
            str(tmp_path / "pat-fingerprint.json"),
        ]
    )

    assert (key_round, pat_round) == (0, 0)
    data = load_yaml_from_path(str(path))
    assert await decrypt_field(data["repositories"][0]["password"], new_private) == "ghp_brand_new"
    assert await opens_with(data["config"]["age-private-key"], new_private)


@pytest.mark.asyncio
async def test_a_second_pat_round_does_nothing_and_still_exits_clean(projects_repo: Path) -> None:
    """The other half: the gate opens on the token, so it closes again once that token is in.

    Without this the fix for the round above would trade a no-op for a re-encryption on every
    run, and "run it twice, the second time does nothing" is a promise of this tool.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    await run_round(directory, old_private, new_private, new_pat="ghp_new", dry_run=False)
    ciphertext_after_the_first_round = load_yaml_from_path(str(path))["repositories"][0]["password"]
    second = await run_round(directory, old_private, new_private, new_pat="ghp_new", dry_run=False)

    assert second.converted == []
    assert (
        second.skipped[0].skipped == "the repository password already holds this PAT (2 of 2 fields sit on the new key)"
    )
    assert broken(second, pat_round=True) == []
    assert load_yaml_from_path(str(path))["repositories"][0]["password"] == ciphertext_after_the_first_round


@pytest.mark.asyncio
async def test_a_second_pat_round_leaves_the_fingerprint_of_the_first_alone(
    projects_repo: Path, tmp_path: Path
) -> None:
    """The same guard on the other entry point, which saves its own fingerprint on the same line.

    A second PAT round finds the token already in place, so it converts nothing either.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_brand_new\n")
    await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "pat-fingerprint.json"
    arguments = [
        "--ja",
        "--projects",
        str(projects_repo / "projects"),
        "--pat-file",
        str(tmp_path / "pat.txt"),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(fingerprint),
    ]

    first = await main_replace_pat(arguments)
    after_the_first_round = fingerprint.read_text()
    second = await main_replace_pat(arguments)

    assert first == 0
    assert second == 0
    assert len(Fingerprint.load(fingerprint).fields) == 2
    assert fingerprint.read_text() == after_the_first_round


def test_already_converted_is_only_harmless_in_the_key_round() -> None:
    result = RoundResult(skipped=[ProjectRound(path=Path("een.yaml"), skipped="already converted (2 of 2 fields)")])

    assert broken(result) == []
    assert len(broken(result, pat_round=True)) == 1


# ---------------------------------------------------------------------------
# the shapes the key round leaves behind for the PAT round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_project_without_a_repository_password_is_no_finding_in_the_pat_round(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The documented order over a project that has nothing for the PAT round to replace.

    Its skip has to count as harmless, or the last step of the cutover ends on "FAIL 1 project
    files were skipped with a real problem" while nothing is wrong -- the same false verdict as
    the one the previous round fixed, with the sign flipped.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_brand_new\n")
    path = await _write_project(projects_repo / "projects", "een", old_public, passwords=(None,))
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    keys = [
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--projects",
        str(projects_repo / "projects"),
        "--ja",
    ]

    key_round = await main_rotate_keys([*keys, "--fingerprint", str(tmp_path / "key-fingerprint.json")])
    after_the_key_round = path.read_text()
    pat_round = await main_replace_pat(
        [*keys, "--pat-file", str(tmp_path / "pat.txt"), "--fingerprint", str(tmp_path / "pat-fingerprint.json")]
    )

    assert (key_round, pat_round) == (0, 0)
    assert "no repository password to replace" in capsys.readouterr().out
    assert path.read_text() == after_the_key_round
    assert await opens_with(load_yaml_from_path(str(path))["config"]["age-private-key"], new_private)


@pytest.mark.asyncio
async def test_the_pat_dry_run_exits_clean_once_the_round_is_done(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A dry run is what the operator uses to check before running, and afterwards to check again.

    It judges the preview with the same eyes as the round itself: the skips a finished PAT round
    produces are harmless, so the rerun has to say so instead of reporting a problem.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_brand_new\n")
    path = await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    keys = [
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--projects",
        str(projects_repo / "projects"),
        "--ja",
        "--pat-file",
        str(tmp_path / "pat.txt"),
    ]
    await main_replace_pat([*keys, "--fingerprint", str(tmp_path / "pat-fingerprint.json")])
    after_the_round = path.read_text()
    dry_fingerprint = tmp_path / "dry-fingerprint.json"

    code = await main_replace_pat([*keys, "--dry-run", "--fingerprint", str(dry_fingerprint)])

    assert code == 0
    assert "Dry run: nothing was changed." in capsys.readouterr().out
    assert not dry_fingerprint.exists()
    assert path.read_text() == after_the_round


@pytest.mark.asyncio
async def test_every_repository_password_gets_the_new_pat_and_not_just_the_first(
    projects_repo: Path,
) -> None:
    """A project with more than one repository: the token is per repository, so all of them move.

    One left behind is invisible in the round's own output -- it converts, it commits, it exits
    0 -- and shows up as a project that cannot reach that one repository after the old PAT is
    revoked.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public, passwords=("ghp_first_repo", "ghp_second_repo"))
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    result = await run_round(directory, old_private, new_private, new_pat="ghp_brand_new", dry_run=False)

    assert result.replaced_fields == [
        f"{path}#repositories[0].password",
        f"{path}#repositories[1].password",
    ]
    assert not result.fingerprint_before.compare(result.fingerprint_after, replaced=result.replaced_fields)
    data = load_yaml_from_path(str(path))
    for index in (0, 1):
        assert await decrypt_field(data["repositories"][index]["password"], new_private) == "ghp_brand_new"
