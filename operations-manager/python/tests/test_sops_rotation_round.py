"""Tests for scripts/sops_rotation.py: the round over the SOPS files and the loose env values.

Three properties carry the whole cutover, and a "it ran" check catches none of them:

* the plan tells "to do" from "already done" from "broken" by MEASURING, which is what makes a
  second run a no-op rather than a failure on the fingerprint;
* the fingerprint covers the same SET of fields before and after, or the comparison invents
  deviations the moment a previous round had converted a few;
* a skipped file makes the final check fail by name, and the count has to add up across BOTH
  fingerprints (this repo's and the projects one), not just one.

The env-line side is exercised on real ciphertext; ``sops`` itself is only needed for the tests
marked as such, and they skip without the binary.
"""

from __future__ import annotations

import base64
import shutil
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

import sops_rotation as tool  # noqa: E402
from key_rotation import Fingerprint, opens_with, sha256_of  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="requires the age binary")


@pytest.fixture(autouse=True)
def no_own_projects(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point the repo's own ``projects/`` at an empty directory for every test here.

    This repo really does carry a project file with a platform-keyed field, so without this every
    test below would also measure that one and the numbers would move whenever it changes. The
    tests that are ABOUT that directory override this with their own.
    """
    empty = tmp_path_factory.mktemp("no-projects")
    with patch.object(tool, "OWN_PROJECTS", empty):
        yield empty


async def _env_file(path: Path, values: dict[str, str], public_key: str) -> Path:
    """An env file shaped like the real ones: a ``KEY=base64+age:...`` line per value."""
    lines = []
    for key, plaintext in values.items():
        block = await encrypt_age_content(plaintext, public_key)
        lines.append(f"{key}={BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}")
    path.write_text("\n".join([*lines, "PLAIN_VALUE=not-a-secret", ""]))
    return path


@pytest.mark.asyncio
async def test_the_plan_lists_a_field_that_still_sits_on_the_old_key(tmp_path: Path) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)

    with patch.object(tool, "sops_files_for", return_value=[]):
        plan = await tool.build_plan([path], old_private, new_private, old_public)

    assert [field.key for field in plan.env] == ["GIT_PROJECTS_SERVER_PASSWORD"]
    assert plan.already == []
    assert plan.closed == []
    assert plan.total == 1


@pytest.mark.asyncio
async def test_the_plan_calls_an_already_converted_field_done(tmp_path: Path) -> None:
    """This is what makes a second run a no-op instead of a failure on the fingerprint.

    A loose ``base64+age:`` value does not carry its recipient in the text, so without the
    decryption attempt the plan would list it as "to do" again and then strand.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"GIT_ARGO_APPLICATIONS_PASSWORD": "hunter2"}, new_public)

    with patch.object(tool, "sops_files_for", return_value=[]):
        plan = await tool.build_plan([path], old_private, new_private, "age1irrelevant")

    assert plan.env == []
    assert plan.total == 0
    assert [name.endswith("#GIT_ARGO_APPLICATIONS_PASSWORD") for name in plan.already] == [True]


@pytest.mark.asyncio
async def test_the_plan_reports_a_field_that_opens_with_neither_key(tmp_path: Path) -> None:
    """A value only a third key opens is a finding, not something to convert blindly."""
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    _stranger_private, stranger_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "x"}, stranger_public)

    with patch.object(tool, "sops_files_for", return_value=[]):
        plan = await tool.build_plan([path], old_private, new_private, "age1irrelevant")

    assert plan.env == []
    assert len(plan.closed) == 1


@pytest.mark.asyncio
async def test_the_round_converts_the_env_line_in_place(tmp_path: Path) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"A_PASSWORD": "first", "B_PASSWORD": "second"}, old_public)
    before = path.read_text()

    with patch.object(tool, "sops_files_for", return_value=[]):
        plan = await tool.build_plan([path], old_private, new_private, old_public)
        await tool.run_rotation(plan, old_private, new_private, old_public, new_public)

    after = path.read_text()
    assert after != before
    assert "PLAIN_VALUE=not-a-secret" in after
    assert len(after.splitlines()) == len(before.splitlines())
    fields = tool.all_env_fields([path])
    measured, closed = await tool.fingerprint_now([], fields, new_private)
    assert closed == []
    assert set(measured.fields.values()) == {sha256_of("first"), sha256_of("second")}


@pytest.mark.asyncio
async def test_the_fingerprint_covers_the_same_set_before_and_after_a_partial_round(
    tmp_path: Path,
) -> None:
    """One field already on B, one still on A: the comparison must find no deviation.

    With a single key per measurement the "before" set would hold one field and the "after" set
    two, and the comparison would report "field appeared" on a perfectly healthy tree.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = tmp_path / ".env"
    old_block = await encrypt_age_content("still-old", old_public)
    new_block = await encrypt_age_content("already-new", new_public)
    path.write_text(
        f"A_PASSWORD={BASE64_AGE_PREFIX}{base64.b64encode(old_block.encode()).decode()}\n"
        f"B_PASSWORD={BASE64_AGE_PREFIX}{base64.b64encode(new_block.encode()).decode()}\n"
    )

    fields = tool.all_env_fields([path])
    before, closed_before = await tool.fingerprint_now([], fields, old_private, new_private)
    assert closed_before == []
    assert len(before.fields) == 2

    with patch.object(tool, "sops_files_for", return_value=[]):
        plan = await tool.build_plan([path], old_private, new_private, old_public)
        assert len(plan.env) == 1
        assert len(plan.already) == 1
        await tool.run_rotation(plan, old_private, new_private, old_public, new_public)

    after, closed_after = await tool.fingerprint_now([], tool.all_env_fields([path]), new_private)
    assert closed_after == []
    assert before.compare(after) == []


@pytest.mark.asyncio
async def test_fingerprint_now_reports_a_field_no_key_opens(tmp_path: Path) -> None:
    _stranger_private, stranger_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"A_PASSWORD": "x"}, stranger_public)

    measured, closed = await tool.fingerprint_now([], tool.all_env_fields([path]), new_private)

    assert measured.fields == {}
    assert len(closed) == 1


def test_the_expected_count_is_the_sum_of_the_fingerprints_that_exist(tmp_path: Path) -> None:
    """The final check walks four places, converted by two tools with a fingerprint each.

    Passing only one of them would compare a part against a whole and always deviate.
    """
    repo = tmp_path / "repo.json"
    projects = tmp_path / "projects.json"
    Fingerprint(fields={"a": "1", "b": "2"}).save(repo)
    Fingerprint(fields={"c": "3"}).save(projects)

    assert tool.expected_count([repo, projects]) == 3
    assert tool.expected_count([repo]) == 2
    assert tool.expected_count([tmp_path / "absent.json"]) is None


@pytest.mark.asyncio
async def test_the_final_check_names_a_skipped_env_field(tmp_path: Path) -> None:
    """A field left on the old key has to show up by name, not as a silent pass."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"FORGOTTEN_PASSWORD": "x"}, old_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "env_paths", return_value=[path]),
    ):
        check = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None)

    assert not check.clean
    assert len(check.still_opens_with_old) == 1
    assert check.still_opens_with_old[0].endswith("#FORGOTTEN_PASSWORD")


@pytest.mark.asyncio
async def test_the_final_check_walks_the_project_files_when_given_a_directory(tmp_path: Path) -> None:
    """Without --projects the fourth place is not walked; with it, it is."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    projects = tmp_path / "projects"
    projects.mkdir()
    block = await encrypt_age_content("ghp_token", old_public)
    (projects / "een.yaml").write_text(
        "name: een\nrepositories:\n  - name: main-repo\n    password: "
        f"{BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}\n"
    )

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "env_paths", return_value=[]),
    ):
        without = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None)
        with_projects = await tool.run_final_check(old_private, new_private, old_public, new_public, projects, None)

    assert without.counted == 0
    assert with_projects.counted == 1
    assert with_projects.still_opens_with_old[0].endswith("#repositories[0].password")


@pytest.mark.asyncio
async def test_the_final_check_fails_when_the_count_does_not_match(tmp_path: Path) -> None:
    """All fields converted and readable, one fewer than the fingerprint: not clean."""
    old_private, _old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"A_PASSWORD": "x"}, new_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "env_paths", return_value=[path]),
    ):
        check = await tool.run_final_check(old_private, new_private, "age1a", "age1b", None, 2)

    assert check.still_opens_with_old == []
    assert check.does_not_open_with_new == []
    assert not check.count_matches
    assert not check.clean


def test_renaming_is_a_no_op_when_the_keys_already_sit_in_place(capsys: pytest.CaptureFixture) -> None:
    """84 references point at security/key.txt, so the name is the migration."""
    tool.CANONICAL_OLD.parent.mkdir(parents=True, exist_ok=True)
    if not tool.CANONICAL_OLD.is_file() or not tool.CANONICAL_NEW.is_file():
        pytest.skip("no local security/ keys to check the no-op path against")
    tool.rename_keys(tool.CANONICAL_OLD, tool.CANONICAL_NEW, yes=True)
    assert "already sit under their fixed names" in capsys.readouterr().out


def test_renaming_moves_both_files_into_place(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    old = tmp_path / "elsewhere-old.txt"
    new = tmp_path / "elsewhere-new.txt"
    old.write_text("old\n")
    new.write_text("new\n")
    target_old = tmp_path / "security" / "old_key.txt"
    target_new = tmp_path / "security" / "key.txt"
    target_old.parent.mkdir()

    with (
        patch.object(tool, "CANONICAL_OLD", target_old),
        patch.object(tool, "CANONICAL_NEW", target_new),
    ):
        tool.rename_keys(old, new, yes=True)

    assert target_old.read_text() == "old\n"
    assert target_new.read_text() == "new\n"
    assert not old.exists()
    assert "Renamed." in capsys.readouterr().out


def test_renaming_does_nothing_without_a_yes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    old = tmp_path / "elsewhere-old.txt"
    new = tmp_path / "elsewhere-new.txt"
    old.write_text("old\n")
    new.write_text("new\n")
    target_old = tmp_path / "security" / "old_key.txt"
    target_new = tmp_path / "security" / "key.txt"
    target_old.parent.mkdir()

    with (
        patch.object(tool, "CANONICAL_OLD", target_old),
        patch.object(tool, "CANONICAL_NEW", target_new),
        patch("builtins.input", return_value="no"),
    ):
        tool.rename_keys(old, new, yes=False)

    assert old.exists()
    assert not target_old.exists()
    assert "Do this by hand" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_same_key_twice_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Rotating onto the same key would report success while changing nothing meaningful."""
    private, _public = generate_sops_key_pair()
    key_file = tmp_path / "key.txt"
    key_file.write_text(f"{private}\n")

    code = await tool.main(["--ja", "--old-key", str(key_file), "--new-key", str(key_file)])

    assert code == 2
    assert "the same key" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_missing_key_file_names_the_path(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    absent = tmp_path / "old_key.txt"
    code = await tool.main(["--ja", "--dry-run", "--old-key", str(absent)])
    assert code == 2
    assert str(absent) in capsys.readouterr().err


@pytest.mark.asyncio
async def test_verify_refuses_without_a_fingerprint(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """--verify is meant to be runnable months later, so a missing baseline is a hard error."""
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")

    code = await tool.main(
        [
            "--ja",
            "--verify",
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--fingerprint",
            str(tmp_path / "absent.json"),
        ]
    )

    assert code == 2
    assert "no fingerprint to check against" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_removing_the_old_key_is_refused_without_the_project_files(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The plan's acceptance criterion: it refuses to delete A while projects may still be on A."""
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    old_file = tmp_path / "old_key.txt"
    old_file.write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "env_paths", return_value=[]),
    ):
        code = await tool.main(
            [
                "--ja",
                "--remove-old-key",
                "--old-key",
                str(old_file),
                "--new-key",
                str(tmp_path / "key.txt"),
                "--fingerprint",
                str(tmp_path / "absent.json"),
            ]
        )

    assert code == 1
    assert "REFUSED" in capsys.readouterr().err
    assert old_file.exists()


@pytest.mark.asyncio
async def test_removing_the_old_key_happens_once_the_check_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    old_file = tmp_path / "old_key.txt"
    old_file.write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    projects = tmp_path / "projects"
    projects.mkdir()

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "env_paths", return_value=[]),
    ):
        code = await tool.main(
            [
                "--ja",
                "--remove-old-key",
                "--projects",
                str(projects),
                "--old-key",
                str(old_file),
                "--new-key",
                str(tmp_path / "key.txt"),
                "--fingerprint",
                str(tmp_path / "absent.json"),
            ]
        )

    assert code == 0
    assert not old_file.exists()
    assert "Old key removed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# this repo's own projects/ directory
# ---------------------------------------------------------------------------


async def _own_project(directory: Path, name: str, public_key: str) -> Path:
    """A project file as it sits in this repo's ``projects/``: one repository password."""
    block = await encrypt_age_content("ghp_repository_token", public_key)
    path = directory / f"{name}.yaml"
    path.write_text(
        "schema-version: 2\n"
        f"name: {name}\n"
        "repositories:\n"
        "  - name: main-repo\n"
        "    url: https://github.com/example/app.git\n"
        "    username: git\n"
        f"    password: {BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}\n"
    )
    return path


@pytest.mark.asyncio
async def test_this_repos_own_project_file_is_part_of_the_plan(tmp_path: Path) -> None:
    """Measured: projects/simple-example.yaml holds a repository password on the platform key.

    It is a project file, so the engine converts it, but it lives HERE. Pointing
    rotate-project-keys.py at a clone of zad-projects would never reach it.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    own = tmp_path / "projects"
    own.mkdir()
    await _own_project(own, "simple-example", old_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "OWN_PROJECTS", own),
    ):
        plan = await tool.build_plan([], old_private, new_private, old_public)

    assert [report.path.name for report in plan.projects] == ["simple-example.yaml"]
    assert plan.projects[0].fields == ["repositories[0].password"]
    assert plan.total == 1


@pytest.mark.asyncio
async def test_this_repos_own_project_file_is_converted_and_fingerprinted(tmp_path: Path) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    own = tmp_path / "projects"
    own.mkdir()
    path = await _own_project(own, "simple-example", old_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "OWN_PROJECTS", own),
    ):
        before, closed_before = await tool.fingerprint_now([], [], old_private, new_private)
        plan = await tool.build_plan([], old_private, new_private, old_public)
        await tool.run_rotation(plan, old_private, new_private, old_public, new_public)
        after, closed_after = await tool.fingerprint_now([], [], old_private, new_private)

    assert closed_before == []
    assert closed_after == []
    assert len(before.fields) == 1
    assert before.compare(after) == []
    data = load_yaml_from_path(str(path))
    assert await opens_with(data["repositories"][0]["password"], new_private)
    assert not await opens_with(data["repositories"][0]["password"], old_private)


@pytest.mark.asyncio
async def test_the_final_check_walks_this_repos_own_project_file(tmp_path: Path) -> None:
    """Without this the final check would pass with a field in this repo still on the old key."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    own = tmp_path / "projects"
    own.mkdir()
    await _own_project(own, "simple-example", old_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "env_paths", return_value=[]),
        patch.object(tool, "OWN_PROJECTS", own),
    ):
        check = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None)

    assert check.counted == 1
    assert not check.clean
    assert check.still_opens_with_old[0].endswith("#repositories[0].password")
