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
from opi.utils.yaml_util import load_yaml_from_path, save_yaml_to_path
from tests.documented_commands import documented_lines, flags

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import project_rotation as round_tool  # noqa: E402
import sops_rotation as final_check_tool  # noqa: E402
from key_rotation import (  # noqa: E402
    PROJECT_FIELD_PRIVATE_KEY,
    Fingerprint,
    MissingKey,
    ProjectRound,
    decrypt_field,
    opens_with,
    set_project_field,
    sha256_of,
)
from project_rotation import (  # noqa: E402
    KEY_FINGERPRINT,
    RoundResult,
    broken,
    build_key_parser,
    build_pat_parser,
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


async def _repositories(platform_public: str, passwords: tuple[str | None, ...], *, armored: bool = False) -> str:
    """The ``repositories:`` block, one entry per element of ``passwords``.

    ``None`` is a repository that carries no password at all. That is not a contrived shape:
    two of the three repositories in this repo's own ``projects/simple-example.yaml`` look
    exactly like that, and a project made only of them has nothing for the PAT round to do.

    ``armored`` is the second storage form the same field occurs in: an AGE block under a
    ``|-`` scalar instead of one ``base64+age:`` line. Measured on the projects repo: 19 of the
    53 files store the password that way, the eight in ``local-old/`` all of them.
    """
    lines: list[str] = []
    for index, password in enumerate(passwords):
        lines.append(f"  - name: {'main-repo' if index == 0 else f'repo-{index + 1}'}")
        lines.append("    url: https://github.com/example/app.git")
        if password is not None:
            block = await encrypt_age_content(password, platform_public)
            lines.append("    username: git")
            if armored:
                lines.append("    password: |-")
                lines.extend(_indent(block, 6).splitlines())
            else:
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
    armored: bool = False,
) -> Path:
    """One project file shaped like the real ones: block-scalar key, repo password per ``armored``."""
    project_private, project_public = generate_sops_key_pair()
    path = directory / f"{name}.yaml"
    path.write_text(
        PROJECT_TEMPLATE.format(
            name=name,
            repositories=await _repositories(platform_public, passwords, armored=armored),
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
async def test_a_round_commits_but_pushes_nothing(projects_repo: Path, tmp_path: Path) -> None:
    """VERIFY-1 is the go/no-go moment BEFORE anything is pushed, so the round may not push itself.

    The monthly exercise leans on this hardest: it would run PREPARE and VERIFY-1 on a throwaway
    key and throw the clone away, and a push there puts a rotation nobody asked for on the real
    projects repo. That exercise is still a proposal, so this is the only thing holding the
    boundary it will need. The round prints "Nothing pushed" whatever it does, so this measures
    the remote and not the output.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    directory = projects_repo / "projects"
    for name in ("een", "twee"):
        await _write_project(directory, name, old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    _git(projects_repo, "remote", "add", "origin", str(origin))
    _git(projects_repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    remote_before = _git(origin, "rev-parse", "refs/heads/main")

    result = await run_round(directory, old_private, new_private, dry_run=False)

    # Without this, a round that converted nothing would pass the line below for free.
    assert result.committed == ["een.yaml", "twee.yaml"]
    assert _git(origin, "rev-parse", "refs/heads/main") == remote_before, "the round pushed to origin"


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
    The second half of this test is there because a count check that is not running at all also
    prints no complaint: with one field taken out of the record, the same command has to go red.
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
        patch.object(final_check_tool, "loose_paths", return_value=[]),
        # The coverage guard reads the REAL tree; the three patches around it point the check at
        # this temporary one. Without an empty inventory every loose-value file of the repo itself
        # would come out as "nothing converts it", because the selection no longer names them.
        patch.object(final_check_tool, "files_with_ciphertext", return_value={}),
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
async def test_a_second_key_round_records_the_same_fields_as_the_first(projects_repo: Path, tmp_path: Path) -> None:
    """Running the tool twice is a promise of this tool, and the second run converts nothing.

    Why the record survives that is in ``save_fingerprint``. It is re-measured over the whole
    collection rather than held onto, so what has to be equal is the SET of fields and their
    hashes; only ``created`` differs, and that stamp says when the collection was last measured.

    This goes through the entry point on purpose: ``run_round`` sits UNDER the layer that saves.
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
    after_the_first_round = Fingerprint.load(fingerprint).fields
    second = await main_rotate_keys(arguments)

    assert first == 0
    assert second == 0
    assert len(after_the_first_round) == 2
    assert Fingerprint.load(fingerprint).fields == after_the_first_round


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
async def test_a_second_pat_round_records_the_same_fields_as_the_first(projects_repo: Path, tmp_path: Path) -> None:
    """The same guard on the other entry point, which saves its own fingerprint on the same line.

    A second PAT round finds the token already in place, so it converts nothing either -- and
    the hashes have to be the NEW token's, or the record does not describe the files as they
    now are.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_brand_new\n")
    path = await _write_project(projects_repo / "projects", "een", old_public)
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
    after_the_first_round = Fingerprint.load(fingerprint).fields
    second = await main_replace_pat(arguments)

    assert first == 0
    assert second == 0
    assert len(after_the_first_round) == 2
    assert Fingerprint.load(fingerprint).fields == after_the_first_round
    assert after_the_first_round[f"{path}#repositories[0].password"] == sha256_of("ghp_brand_new")


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


@pytest.mark.asyncio
async def test_a_partial_round_and_its_repair_still_add_up_for_the_final_check(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The documented recovery path: one file unreadable, repair it, run again, then step 8.

    ``run_round`` promises that an unreadable file does not abort the round, so a rotation can
    take two rounds and the final check still has to add up. Why the fingerprint holds the whole
    collection is in ``save_fingerprint``.

    The count after the partial round is 5 and not the 4 the round converted: only the password
    of bbb is unreadable, its project key still opens, and the record says what the collection
    HOLDS rather than what this one round did. The field it could not measure is named, and so
    is the same field when the repair round adds it: the comparison against the earlier record
    lets a field come and go on purpose, so the printed line is the only trace of it.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    for name in ("aaa", "bbb", "ccc"):
        await _write_project(directory, name, old_public)
    broken_path = directory / "bbb.yaml"
    intact = broken_path.read_text()
    broken_path.write_text(intact.replace(intact.split("password: ")[1].split("\n")[0], f"{BASE64_AGE_PREFIX}QUJDREVG"))
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"
    keys = ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]
    arguments = ["--ja", "--projects", str(directory), *keys, "--fingerprint", str(fingerprint)]

    partial = await main_rotate_keys(arguments)
    after_the_partial_round = len(Fingerprint.load(fingerprint).fields)
    partial_output = capsys.readouterr().out
    broken_path.write_text(intact)
    repaired = await main_rotate_keys(arguments)
    repair_output = capsys.readouterr().out
    with (
        patch.object(final_check_tool, "sops_files_for", return_value=[]),
        patch.object(final_check_tool, "loose_paths", return_value=[]),
        patch.object(final_check_tool, "files_with_ciphertext", return_value={}),
        patch.object(final_check_tool, "OWN_PROJECTS", tmp_path / "not-a-directory"),
        patch.object(final_check_tool, "DEFAULT_PROJECTS_FINGERPRINT", fingerprint),
    ):
        step_8 = await final_check_tool.main(
            [
                "--ja",
                "--assert-old-key-dead",
                "--projects",
                str(directory),
                *keys,
                "--fingerprint",
                str(tmp_path / "no-fingerprint-for-this-repo.json"),
            ]
        )
    printed = capsys.readouterr().out

    assert partial == 1
    assert after_the_partial_round == 5
    assert f"opens with neither key: {broken_path}#repositories[0].password" in partial_output
    assert repaired == 0
    assert len(Fingerprint.load(fingerprint).fields) == 6
    # A field entering the collection is allowed here, which is precisely why it has to be
    # said out loud: the guard cannot tell a repaired file from a project that quietly turned up.
    assert f"new in the collection since the last round: {broken_path}#repositories[0].password" in repair_output
    assert "count differs" not in printed
    assert "CLEAN the old key opens nothing, the new key opens everything" in printed
    assert step_8 == 0


@pytest.mark.asyncio
async def test_a_changed_plaintext_stops_the_next_round_instead_of_overwriting_the_record(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The record was written every round and read by none, which made it a tally.

    Nothing compared it: ``expected_count`` takes its ``len()`` and ``--verify`` walked past it.
    So a second round measured the collection afresh, wrote the new hashes over the old ones and
    exited 0 -- and with the earlier hashes gone there was nothing left for the final check to
    disagree with either. This replays exactly that: one password re-encrypted for the NEW key
    with a DIFFERENT plaintext behind it, which is what a round has no business doing silently.

    The record has to survive the refusal. Overwriting it while reporting the deviation would
    destroy the evidence and make the next run green.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"
    arguments = [
        "--ja",
        "--projects",
        str(directory),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(fingerprint),
    ]

    assert await main_rotate_keys(arguments) == 0
    recorded = fingerprint.read_text()
    capsys.readouterr()

    # The field is still perfectly readable with the new key. Only what it SAYS is different,
    # which is the one thing a fingerprint exists to notice.
    path = directory / "een.yaml"
    swapped = await encrypt_age_content("ghp_somebody_elses_token", new_public)
    original = path.read_text()
    path.write_text(
        original.replace(
            original.split("password: ")[1].split("\n")[0],
            f"{BASE64_AGE_PREFIX}{base64.b64encode(swapped.encode()).decode()}",
        )
    )

    code = await main_rotate_keys(arguments)
    printed = capsys.readouterr().out

    assert code == 1
    assert f"the fingerprint recorded in {fingerprint} disagrees" in printed
    assert f"content changed: {path}#repositories[0].password" in printed
    assert fingerprint.read_text() == recorded, "the earlier record is the evidence and has to stand"


@pytest.mark.asyncio
async def test_the_pat_round_expects_the_passwords_to_read_differently_and_the_key_not_to(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The comparison against the record is the same one, minus the fields that are MEANT to move.

    A PAT round rewrites every repository password on purpose, so without ``replaced`` a second
    PAT round with a new token would go red on its own reason for existing. The project key is
    only ever re-encrypted, so it stays on the strict side of the same check.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-pat-fingerprint.json"
    keys = ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]

    first = tmp_path / "first-pat.txt"
    first.write_text("ghp_the_first_new_token\n")
    second = tmp_path / "second-pat.txt"
    second.write_text("ghp_the_second_new_token\n")
    arguments = ["--ja", "--projects", str(directory), *keys, "--fingerprint", str(fingerprint)]

    assert await main_replace_pat([*arguments, "--pat-file", str(first)]) == 0
    after_the_first = Fingerprint.load(fingerprint).fields
    capsys.readouterr()

    assert await main_replace_pat([*arguments, "--pat-file", str(second)]) == 0
    after_the_second = Fingerprint.load(fingerprint).fields
    printed = capsys.readouterr().out

    path = directory / "een.yaml"
    assert "disagrees with what is there now" not in printed
    assert after_the_first[f"{path}#repositories[0].password"] != after_the_second[f"{path}#repositories[0].password"]
    assert (
        after_the_first[f"{path}#{PROJECT_FIELD_PRIVATE_KEY}"]
        == after_the_second[f"{path}#{PROJECT_FIELD_PRIVATE_KEY}"]
    )


@pytest.mark.asyncio
async def test_verify_still_stands_after_the_pat_round_because_both_rounds_share_one_record(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Step 3, then step 7, then the check the feature doc promises still works months later.

    The two rounds are documented without ``--fingerprint``, so this runs them on their defaults,
    which is where the fault was: a record of its own for the PAT round meant nobody read it, and
    ``rotate-sops-key.py --verify --projects`` -- which defaults to the key round's record -- then
    reported "content changed" on every password with nothing wrong. One record for the
    collection keeps that promise, and gives up no check: the password hash has to have MOVED and
    the project key hash has to have stayed put.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"
    repo_record = tmp_path / "fingerprint.json"
    Fingerprint().save(repo_record)
    pat = tmp_path / "pat.txt"
    pat.write_text("ghp_the_new_token\n")
    keys = ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]

    with patch.object(round_tool, "KEY_FINGERPRINT", fingerprint):
        assert await main_rotate_keys(["--ja", "--projects", str(directory), *keys]) == 0
        after_the_key_round = Fingerprint.load(fingerprint).fields
        assert await main_replace_pat(["--ja", "--projects", str(directory), *keys, "--pat-file", str(pat)]) == 0
    after_the_pat_round = Fingerprint.load(fingerprint).fields
    capsys.readouterr()

    with (
        patch.object(final_check_tool, "sops_files_for", return_value=[]),
        patch.object(final_check_tool, "loose_paths", return_value=[]),
        patch.object(final_check_tool, "files_with_ciphertext", return_value={}),
        patch.object(final_check_tool, "OWN_PROJECTS", tmp_path / "not-a-directory"),
        patch.object(final_check_tool, "DEFAULT_PROJECTS_FINGERPRINT", fingerprint),
    ):
        step_7_verify = await final_check_tool.main(
            ["--ja", "--verify", "--projects", str(directory), *keys, "--fingerprint", str(repo_record)]
        )

    printed = capsys.readouterr().out
    assert step_7_verify == 0, "the record the PAT round left behind is the one --verify reads"
    assert "content changed" not in printed
    assert "CLEAN 2 fields readable with the new key and unchanged in content" in printed
    assert (
        after_the_key_round[f"{path}#repositories[0].password"]
        != after_the_pat_round[f"{path}#repositories[0].password"]
    )
    assert (
        after_the_key_round[f"{path}#{PROJECT_FIELD_PRIVATE_KEY}"]
        == after_the_pat_round[f"{path}#{PROJECT_FIELD_PRIVATE_KEY}"]
    )


@pytest.mark.asyncio
async def test_the_combined_quarterly_round_leaves_the_final_check_everything_step_7_would_have(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Step 3 of a quarterly round: the PAT entry alone, on files that still sit on the old key.

    The documented FIRST round runs the key round and then the PAT round, and it is the second
    of those that leaves the record the final check counts. A quarterly round drops step 7, so
    this single pass has to do both halves and leave that record itself -- with the project key
    only re-encrypted, the password replaced, and the old key opening neither.

    Both rounds are documented without ``--fingerprint``, so this runs on the default as well.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_the_quarterly_token\n")
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    project_key_before = await decrypt_field(load_yaml_from_path(str(path))["config"]["age-private-key"], old_private)
    fingerprint = tmp_path / "projects-fingerprint.json"
    keys = ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]

    with patch.object(round_tool, "KEY_FINGERPRINT", fingerprint):
        quarterly = await main_replace_pat(
            ["--ja", "--projects", str(directory), *keys, "--pat-file", str(tmp_path / "pat.txt")]
        )
    capsys.readouterr()

    data = load_yaml_from_path(str(path))
    assert quarterly == 0
    assert await decrypt_field(data["repositories"][0]["password"], new_private) == "ghp_the_quarterly_token"
    assert not await opens_with(data["repositories"][0]["password"], old_private)
    assert await decrypt_field(data["config"]["age-private-key"], new_private) == project_key_before
    assert not await opens_with(data["config"]["age-private-key"], old_private)

    with (
        patch.object(final_check_tool, "sops_files_for", return_value=[]),
        patch.object(final_check_tool, "loose_paths", return_value=[]),
        patch.object(final_check_tool, "files_with_ciphertext", return_value={}),
        patch.object(final_check_tool, "OWN_PROJECTS", tmp_path / "not-a-directory"),
        patch.object(final_check_tool, "DEFAULT_PROJECTS_FINGERPRINT", fingerprint),
    ):
        repo_record = tmp_path / "fingerprint.json"
        Fingerprint().save(repo_record)
        arguments = ["--ja", "--projects", str(directory), *keys, "--fingerprint", str(repo_record)]
        verify = await final_check_tool.main([*arguments, "--verify"])
        printed = capsys.readouterr().out
        final_check = await final_check_tool.main([*arguments, "--assert-old-key-dead"])

    assert verify == 0, "the record the quarterly round leaves is the one --verify reads"
    assert "content changed" not in printed
    assert "CLEAN 2 fields readable with the new key and unchanged in content" in printed
    assert final_check == 0
    assert "2 fields checked" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_project_that_left_the_collection_is_named_and_is_not_a_failure(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The other side of a field coming and going, and it is a deletion.

    The comparison holds a round to the fields both records share, so a project that has been
    deleted since is allowed to drop out -- the check does not walk it any more either, and a
    record that kept the entry would fail the count for a project that no longer exists. That
    makes the printed line the only trace, exactly as with a field that appears: without it the
    record silently shrinks and the next count is the new truth.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    for name in ("blijft", "verdwijnt"):
        await _write_project(directory, name, old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"
    arguments = [
        "--ja",
        "--projects",
        str(directory),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(fingerprint),
    ]

    assert await main_rotate_keys(arguments) == 0
    assert len(Fingerprint.load(fingerprint).fields) == 4
    capsys.readouterr()
    gone = directory / "verdwijnt.yaml"
    gone.unlink()

    code = await main_rotate_keys(arguments)
    printed = capsys.readouterr().out

    assert code == 0
    assert f"no longer in the collection: {gone}#{PROJECT_FIELD_PRIVATE_KEY}" in printed
    assert f"no longer in the collection: {gone}#repositories[0].password" in printed
    assert set(Fingerprint.load(fingerprint).fields) == {
        f"{directory / 'blijft.yaml'}#{PROJECT_FIELD_PRIVATE_KEY}",
        f"{directory / 'blijft.yaml'}#repositories[0].password",
    }


@pytest.mark.parametrize("entry_point", ["the key round", "the PAT round"])
@pytest.mark.asyncio
async def test_the_record_names_the_resolved_path_so_the_other_tool_can_match_it(
    projects_repo: Path, tmp_path: Path, entry_point: str
) -> None:
    """Every key in the record is "<path>#<field>", and ``--verify`` compares those NAMES.

    The count in ``--assert-old-key-dead`` adds up regardless of how the clone was spelled: it
    compares totals. The comparison ``--verify`` runs does not, so a clone addressed once with a
    ``..`` in it and once without would read as a collection that was swapped whole -- every
    field disappeared, every field appeared. Both sides resolve the directory, so the spelling
    cannot decide the verdict.

    Both entry points, because both write this record and the second one writes OVER the first:
    a PAT round that recorded the spelling as typed would leave the collection under names no
    later run matches, and ``content_drift`` compares the names two records share, so it would
    find none to object to.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"
    pat_file = tmp_path / "pat.txt"
    pat_file.write_text("ghp_the_new_token\n")
    detour = directory / ".." / "projects"
    arguments = [
        "--ja",
        "--projects",
        str(detour),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(fingerprint),
    ]

    if entry_point == "the key round":
        code = await main_rotate_keys(arguments)
    else:
        code = await main_replace_pat([*arguments, "--pat-file", str(pat_file)])

    assert code == 0
    assert set(Fingerprint.load(fingerprint).fields) == {
        f"{directory.resolve() / 'een.yaml'}#{PROJECT_FIELD_PRIVATE_KEY}",
        f"{directory.resolve() / 'een.yaml'}#repositories[0].password",
    }


async def _put_another_project_key_in(path: Path, public_key: str) -> None:
    """Give the project a DIFFERENT private key, encrypted for the key it already sits on.

    Written through ``set_project_field`` and the canonical writer, so the file that comes out
    is shaped exactly like the one the round writes: a block scalar, same indentation. The one
    thing that differs is what the field says, which is the whole question here.
    """
    other_private, _other_public = generate_sops_key_pair()
    data = load_yaml_from_path(str(path))
    set_project_field(data, PROJECT_FIELD_PRIVATE_KEY, await encrypt_age_content(other_private, public_key))
    save_yaml_to_path(str(path), data)


@pytest.mark.asyncio
async def test_the_pat_round_stops_when_a_field_it_does_not_replace_has_drifted(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``replaced`` excuses the passwords from the comparison. It excuses nothing else.

    The PAT round is the round that is MEANT to change content, which is exactly why the half
    it may not change needs its own guard: ``config.age-private-key`` is only ever re-encrypted,
    and a project whose key silently reads differently afterwards can no longer open a single one
    of its own secrets. The key round refuses on that and returns 1; this asserts the PAT round
    does the same, both in its verdict and in leaving the earlier record alone.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-pat-fingerprint.json"
    first = tmp_path / "first-pat.txt"
    first.write_text("ghp_the_first_new_token\n")
    second = tmp_path / "second-pat.txt"
    second.write_text("ghp_the_second_new_token\n")
    arguments = [
        "--ja",
        "--projects",
        str(directory),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(fingerprint),
    ]

    assert await main_replace_pat([*arguments, "--pat-file", str(first)]) == 0
    recorded = fingerprint.read_text()
    capsys.readouterr()

    # Perfectly readable with the new key, so no decryption test can see this. Only the record can.
    await _put_another_project_key_in(path, new_public)
    code = await main_replace_pat([*arguments, "--pat-file", str(second)])
    printed = capsys.readouterr().out

    assert code == 1
    assert f"content changed: {path}#{PROJECT_FIELD_PRIVATE_KEY}" in printed
    assert fingerprint.read_text() == recorded, "the earlier record is the evidence and has to stand"


@pytest.mark.asyncio
async def test_a_file_that_is_no_project_file_is_named_and_stays_out_of_the_count(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A stray .yaml in the directory: the round names it, the fingerprint leaves it out.

    ``fingerprint_all`` and the final check both walk ``*.yaml`` and both pass over whatever
    does not parse into a mapping. They have to pass over the SAME files, or the count of the
    one is measured against a different collection than the other and step 8 fails on a
    rotation where nothing is wrong. The round does report it: a file it cannot read, in the
    directory it is converting, is something to look at rather than something to walk past.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    for name in ("een", "twee"):
        await _write_project(directory, name, old_public)
    (directory / "notes.yaml").write_text("")
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"
    keys = ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]

    code = await main_rotate_keys(["--ja", "--projects", str(directory), *keys, "--fingerprint", str(fingerprint)])
    round_output = capsys.readouterr().out
    recorded = Fingerprint.load(fingerprint)
    with (
        patch.object(final_check_tool, "sops_files_for", return_value=[]),
        patch.object(final_check_tool, "loose_paths", return_value=[]),
        patch.object(final_check_tool, "files_with_ciphertext", return_value={}),
        patch.object(final_check_tool, "OWN_PROJECTS", tmp_path / "not-a-directory"),
        patch.object(final_check_tool, "DEFAULT_PROJECTS_FINGERPRINT", fingerprint),
    ):
        step_8 = await final_check_tool.main(
            [
                "--ja",
                "--assert-old-key-dead",
                "--projects",
                str(directory),
                *keys,
                "--fingerprint",
                str(tmp_path / "no-fingerprint-for-this-repo.json"),
            ]
        )
    printed = capsys.readouterr().out

    assert code == 1
    assert "notes.yaml: not a readable project file" in round_output
    assert len(recorded.fields) == 4
    assert [name for name in recorded.fields if "notes.yaml" in name] == []
    assert "4 fields checked" in printed
    assert "count differs" not in printed
    assert step_8 == 0


@pytest.mark.asyncio
async def test_the_round_walks_a_project_file_in_a_subdirectory(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The nested file has to be converted AND to stand in the record the final check counts.

    ``projects/local-old/`` is the subdirectory a flat selection leaves on the old key, and the
    round's own numbers cannot say so: the fingerprint comes out of that same selection.

    The line the operator reads names it with its subdirectory: the bare name was enough while
    the selection was flat, and two files of the same name in two directories now print as the
    same line twice.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    (directory / "local-old").mkdir()
    await _write_project(directory, "hier", old_public)
    nested = await _write_project(directory / "local-old", "oud", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"

    code = await main_rotate_keys(
        [
            "--ja",
            "--projects",
            str(directory),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(fingerprint),
        ]
    )

    assert code == 0
    recorded = Fingerprint.load(fingerprint)
    assert len(recorded.fields) == 4
    assert [name for name in recorded.fields if "local-old" in name] != []
    converted = load_yaml_from_path(str(nested))
    assert await opens_with(converted["config"]["age-private-key"], new_private)
    assert not await opens_with(converted["config"]["age-private-key"], old_private)
    assert "local-old/oud.yaml: config.age-private-key" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_flat_selection_is_stopped_by_the_coverage_guard(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The counter-check on the selection, and on the guard that does not come out of it.

    Flatten ``project_files`` back to a plain glob and the round's own numbers still add up --
    one file converted, one file recorded, "unchanged". What sees it is the inventory: git tracks
    the nested file, it carries real ciphertext, and no worklist names it. The round stops before
    a byte is written.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    (directory / "local-old").mkdir()
    await _write_project(directory, "hier", old_public)
    nested = await _write_project(directory / "local-old", "oud", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"

    with patch.object(round_tool, "project_files", lambda directory: sorted(Path(directory).glob("*.yaml"))):
        code = await main_rotate_keys(
            [
                "--ja",
                "--projects",
                str(directory),
                "--old-key",
                str(tmp_path / "old_key.txt"),
                "--new-key",
                str(tmp_path / "key.txt"),
                "--fingerprint",
                str(fingerprint),
            ]
        )

    assert code == 1
    assert "local-old/oud.yaml" in capsys.readouterr().err
    assert not fingerprint.exists()
    assert await opens_with(load_yaml_from_path(str(nested))["config"]["age-private-key"], old_private)


@pytest.mark.asyncio
async def test_a_tracked_file_with_ciphertext_outside_the_selection_stops_the_round(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``.yml`` is the shape the selection does not reach, and the guard is not the selection.

    A path list can only ever name what someone thought of. This one is checked against what git
    says the tree holds, so a second extension, a file without one and a directory nobody knew
    about all come out the same way: named, and the round does not start.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    await _write_project(directory, "hier", old_public)
    block = await encrypt_age_content("ghp_token", old_public)
    (directory / "oud.yml").write_text(
        "name: oud\nrepositories:\n  - name: main-repo\n    password: "
        f"{BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}\n"
    )
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")

    code = await main_rotate_keys(
        [
            "--ja",
            "--projects",
            str(directory),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(tmp_path / "projects-fingerprint.json"),
        ]
    )

    assert code == 1
    assert "oud.yml" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_round_walks_the_work_tree_of_a_clone_and_not_its_git_directory(
    projects_repo: Path, tmp_path: Path
) -> None:
    """A clone root is an answer --projects survives, and ``.git`` stays out of the walk.

    The walk that reaches one subdirectory reaches every subdirectory, git's own storage
    included. The inventory cannot object there: ``git ls-files`` does not list what is inside
    ``.git``, so a file converted in there would be a field in the count that belongs to no
    project, and a write into the store that holds the way back.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    live = await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    inside_git = projects_repo / ".git" / "een.yaml"
    inside_git.write_text(live.read_text())
    fingerprint = tmp_path / "projects-fingerprint.json"

    code = await main_rotate_keys(
        [
            "--ja",
            "--projects",
            str(projects_repo),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(fingerprint),
        ]
    )

    assert code == 0
    assert len(Fingerprint.load(fingerprint).fields) == 2
    assert await opens_with(load_yaml_from_path(str(live))["config"]["age-private-key"], new_private)
    assert await opens_with(load_yaml_from_path(str(inside_git))["config"]["age-private-key"], old_private)


@pytest.mark.asyncio
async def test_a_repository_password_in_a_block_scalar_goes_along_and_stays_one(
    projects_repo: Path, tmp_path: Path
) -> None:
    """The other storage form of the same field, and the one every nested file uses.

    Measured on the projects repo: 19 of the 53 repository passwords sit in an armored block
    instead of on a ``base64+age:`` line, and all eight files in the subdirectory this round
    started walking are of that shape. It is also the shape where a miss stays quiet:
    ``project_fields`` picks the worklist, the fingerprint AND the final check, so a form it
    stops recognising is converted by nothing and missed by nothing -- 106 fields would become
    87, three times over, and every count would agree with itself.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    directory = projects_repo / "projects"
    path = await _write_project(directory, "een", old_public, armored=True)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    fingerprint = tmp_path / "projects-fingerprint.json"

    code = await main_rotate_keys(
        [
            "--ja",
            "--projects",
            str(directory),
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(fingerprint),
        ]
    )

    assert code == 0
    recorded = Fingerprint.load(fingerprint)
    assert [name for name in recorded.fields if name.endswith("#repositories[0].password")] != []
    data = load_yaml_from_path(str(path))
    assert await decrypt_field(data["repositories"][0]["password"], new_private) == "ghp_repository_token"
    assert not await opens_with(data["repositories"][0]["password"], old_private)
    # Written back as a block scalar and not as one line with \n in it: that is another file.
    assert "password: |" in path.read_text()
    assert "\\n" not in path.read_text()


@pytest.mark.asyncio
async def test_both_entry_points_refuse_a_directory_without_a_project_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An existing directory holding no project file at all: a wrong clone, an empty one.

    That used to be a silent exit 0 over "0 project files", which reads as a finished round to
    whoever runs the step after it. The other half of the same mistake needs no refusal:
    ``<clone>`` instead of ``<clone>/projects`` now finds the files one level down.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text("ghp_new_token\n")
    clone = tmp_path / "zad-projects"
    (clone / "projects").mkdir(parents=True)
    keys = ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]

    key_round = await main_rotate_keys(["--ja", "--projects", str(clone), *keys])
    pat_round = await main_replace_pat(
        ["--ja", "--projects", str(clone), *keys, "--pat-file", str(tmp_path / "pat.txt")]
    )

    printed = capsys.readouterr().err
    assert key_round == 2
    assert pat_round == 2
    assert printed.count(f"FAIL no project files under {clone}") == 2
    assert "Check the path: the documented one is <clone>/projects." in printed


@pytest.mark.asyncio
async def test_a_second_round_does_not_point_at_head_0(
    projects_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A round that converted nothing has nothing to look at before pushing.

    The closing advice names the commits this round made, and the second round makes none.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    await _write_project(projects_repo / "projects", "een", old_public)
    _git(projects_repo, "add", "-A")
    _git(projects_repo, "commit", "-q", "-m", "start")
    arguments = [
        "--ja",
        "--projects",
        str(projects_repo / "projects"),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(tmp_path / "fingerprint.json"),
    ]

    assert await main_rotate_keys(arguments) == 0
    assert "git diff --stat HEAD~1" in capsys.readouterr().out
    assert await main_rotate_keys(arguments) == 0

    second = capsys.readouterr().out
    assert "HEAD~" not in second
    assert "nothing to commit or push" in second
    assert "--assert-old-key-dead" in second
    # Same next step, same two clones: this one knew the projects clone and left the argo one
    # out, which hands the operator a check that walks four of the five places.
    assert "--projects" in second
    assert "--argo-applications" in second


def test_the_documented_project_rounds_parse_and_share_one_record() -> None:
    """The half of the operator's script in the feature doc that this module owns.

    ``test_every_documented_invocation_parses_and_the_final_check_walks_the_projects`` walks the
    ``rotate-sops-key.py`` lines; these four were read by nothing. Neither round is documented
    with ``--fingerprint``, so both run on their default, and that default has to be the file
    the final check counts and ``--verify --projects`` compares against -- which is what makes
    the bare step-8 command add up and what keeps step 7 from leaving a verify that reports
    "content changed" on every password. Both records covered the whole collection and were
    the same size, so a second file never halved anything; it was simply read by nobody.
    """
    documented = documented_lines("rotate-project-keys.py", "replace-git-pat.py")
    key_lines = [line for line in documented if "rotate-project-keys.py" in line]
    pat_lines = [line for line in documented if "replace-git-pat.py" in line]

    assert len(key_lines) == 2, "step 3 is a dry run and then the real one"
    assert len(pat_lines) == 2, "step 7 is a dry run and then the real one"
    clones = set()
    for line in key_lines:
        arguments = build_key_parser().parse_args(flags(line))
        assert arguments.fingerprint == str(KEY_FINGERPRINT)
        assert arguments.projects, f"the round has nowhere to look: {line}"
        clones.add(arguments.projects)
    for line in pat_lines:
        arguments = build_pat_parser().parse_args(flags(line))
        assert arguments.fingerprint == str(KEY_FINGERPRINT)
        assert arguments.projects, f"the round has nowhere to look: {line}"
        clones.add(arguments.projects)
    # One record naming every field by its PATH, so two rounds on two clone locations share no
    # name at all and the comparison between them silently checks nothing.
    assert len(clones) == 1, f"step 3 and step 7 have to run on the same clone location: {sorted(clones)}"


def test_the_documented_quarterly_round_swaps_the_script_and_nothing_else() -> None:
    """Step 3 says a quarterly round runs those same two lines with ``replace-git-pat.py``.

    "Dezelfde vlaggen, dezelfde clone" is a claim about the OTHER parser. A flag that only the
    key round knows turns that substitution into "unrecognized arguments" on the day someone
    follows the doc, and step 7 -- which the same sentence declares redundant -- is by then the
    round that would have caught it.
    """
    key_lines = documented_lines("rotate-project-keys.py")

    assert len(key_lines) == 2, "step 3 is a dry run and then the real one"

    for line in key_lines:
        as_written = build_key_parser().parse_args(flags(line))
        try:
            substituted = build_pat_parser().parse_args(flags(line))
        except SystemExit:
            pytest.fail(f"replace-git-pat.py does not take step 3 as documented: {line}")
        assert substituted.projects == as_written.projects
        assert substituted.fingerprint == as_written.fingerprint
        assert substituted.dry_run == as_written.dry_run
