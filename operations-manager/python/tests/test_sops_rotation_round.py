"""Tests for scripts/sops_rotation.py: the round over the SOPS files and the loose env values.

Four properties carry the whole cutover, and a "it ran" check catches none of them:

* a converted SOPS file opens with B, no longer opens with A, and its ciphertext CHANGED -- the
  third is what separates ``sops rotate`` from ``sops updatekeys``;
* the plan tells "to do" from "already done" from "broken" by MEASURING, which is what makes a
  second run a no-op rather than a failure on the fingerprint;
* the fingerprint covers the same SET of fields before and after, or the comparison invents
  deviations the moment a previous round had converted a few;
* a skipped file makes the final check fail by name, and the count has to add up across BOTH
  fingerprints (this repo's and the projects one), not just one.

Both halves run on real ciphertext, made by the encryptor OPI itself uses. ``sops`` is needed
only for the SOPS half, so those tests carry ``needs_sops`` and skip without the binary.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml
from opi.utils.age import BASE64_AGE_PREFIX, encrypt_age_content
from opi.utils.sops import encrypt_to_sops_files, generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_path
from tests.documented_commands import BARE_PYTHON, LAUNCHER, documented_lines, flags

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import sops_rotation as tool  # noqa: E402
from key_rotation import (  # noqa: E402
    AGE_KEY_MARKER,
    ConversionFailed,
    FinalCheck,
    Fingerprint,
    decrypt_field,
    files_with_ciphertext,
    loose_values,
    opens_with,
    read_key,
    sha256_of,
    sops_files,
    sops_files_for,
    sops_plaintext,
    sops_recipients,
    sops_rotate,
)
from project_rotation import KEY_FINGERPRINT, save_fingerprint  # noqa: E402

#: The real default, taken before the autouse fixture below patches it away for every test.
REAL_PROJECTS_FINGERPRINT = tool.DEFAULT_PROJECTS_FINGERPRINT

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="requires the age binary")

#: The SOPS half of the round needs the binary itself. The env-line half does not, so it is a
#: marker per test rather than a second module-level skip.
needs_sops = pytest.mark.skipif(shutil.which("sops") is None, reason="requires the sops binary")

_ENC_VALUE = re.compile(r"ENC\[[^\]]*\]")


def _sops_file(directory: Path, name: str, body: str, public_key: str) -> Path:
    """A real SOPS file for one recipient, made by the same encryptor OPI itself uses."""
    (directory / f"{name}.to-sops.yaml").write_text(body)
    encrypt_to_sops_files(str(directory), public_key)
    return directory / f"{name}.sops.yaml"


async def _project_file(directory: Path, name: str, public_key: str) -> Path:
    """One project file with a repository password on the given key.

    ``--projects`` refuses a directory without a single project file, so a test that points at an
    empty one measures that refusal instead of what it came for.
    """
    block = await encrypt_age_content("ghp_token", public_key)
    encoded = base64.b64encode(block.encode()).decode()
    path = directory / f"{name}.yaml"
    path.write_text(f"name: {name}\nrepositories:\n  - name: main-repo\n    password: {BASE64_AGE_PREFIX}{encoded}\n")
    return path


def _encrypted_values(path: Path) -> list[str]:
    return _ENC_VALUE.findall(path.read_text())


def _selecting_from(tree: Path) -> AbstractContextManager[Any]:
    """Point the tool's file selection at a temporary tree, keeping the real recipient match.

    Patching the SELECTION and not the result: the recipient comparison itself stays live, so a
    round that stopped honouring it still goes red.
    """
    return patch.object(tool, "sops_files_for", side_effect=lambda _tree, public: sops_files_for(tree, public))


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


@pytest.fixture(autouse=True)
def no_coverage_sweep() -> Iterator[None]:
    """Give the coverage guard an empty inventory for every test here.

    ``coverage_gaps()`` reads the REAL tree, and the tests below point the tool's file selection
    at a temporary one. Without this every final check would report the repo's own six loose-value
    files as uncovered, because the selection they patched no longer names them. The logic that
    decides what "covered" means stays live; only the inventory is emptied, and the tests that are
    ABOUT the guard bring their own.
    """
    with patch.object(tool, "files_with_ciphertext", return_value={}):
        yield


@pytest.fixture(autouse=True)
def no_projects_fingerprint(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point the default projects fingerprint at a path that does not exist.

    ``--projects-fingerprint`` defaults to ``security/projects-fingerprint.json``, and on a
    machine that has really run a rotation that file is there. Without this, every final check
    below would take ITS field count as the expected one and go red on a number no test set up.
    The test that is ABOUT that default overrides this with its own.
    """
    absent = tmp_path_factory.mktemp("no-projects-fingerprint") / "projects-fingerprint.json"
    with patch.object(tool, "DEFAULT_PROJECTS_FINGERPRINT", absent):
        yield absent


async def _base64_value(plaintext: str, public_key: str) -> str:
    """One ``base64+age:`` value, made the way ZAD makes them."""
    block = await encrypt_age_content(plaintext, public_key)
    return f"{BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}"


async def _env_file(path: Path, values: dict[str, str], public_key: str) -> Path:
    """An env file shaped like the real ones: a ``KEY=base64+age:...`` line per value."""
    lines = [f"{key}={await _base64_value(plaintext, public_key)}" for key, plaintext in values.items()]
    path.write_text("\n".join([*lines, "PLAIN_VALUE=not-a-secret", ""]))
    return path


@pytest.mark.asyncio
async def test_the_plan_lists_a_field_that_still_sits_on_the_old_key(tmp_path: Path) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)

    with patch.object(tool, "sops_files_for", return_value=[]):
        plan = await tool.build_plan([path], old_private, new_private, old_public)

    assert [field.key for field in plan.loose] == ["GIT_PROJECTS_SERVER_PASSWORD"]
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

    assert plan.loose == []
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

    assert plan.loose == []
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
    fields = tool.all_loose_values([path])
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

    fields = tool.all_loose_values([path])
    before, closed_before = await tool.fingerprint_now([], fields, old_private, new_private)
    assert closed_before == []
    assert len(before.fields) == 2

    with patch.object(tool, "sops_files_for", return_value=[]):
        plan = await tool.build_plan([path], old_private, new_private, old_public)
        assert len(plan.loose) == 1
        assert len(plan.already) == 1
        await tool.run_rotation(plan, old_private, new_private, old_public, new_public)

    after, closed_after = await tool.fingerprint_now([], tool.all_loose_values([path]), new_private)
    assert closed_after == []
    assert before.compare(after) == []


@pytest.mark.asyncio
async def test_fingerprint_now_reports_a_field_no_key_opens(tmp_path: Path) -> None:
    _stranger_private, stranger_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"A_PASSWORD": "x"}, stranger_public)

    measured, closed = await tool.fingerprint_now([], tool.all_loose_values([path]), new_private)

    assert measured.fields == {}
    assert len(closed) == 1


def test_the_expected_count_is_the_sum_of_the_fingerprints_that_exist(tmp_path: Path) -> None:
    """The final check walks five places, converted by two tools with a fingerprint each.

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
        patch.object(tool, "loose_paths", return_value=[path]),
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
    argo = tmp_path / "zad-argo-user-applications"
    argo.mkdir()
    block = await encrypt_age_content("ghp_token", old_public)
    (projects / "een.yaml").write_text(
        "name: een\nrepositories:\n  - name: main-repo\n    password: "
        f"{BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}\n"
    )

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
    ):
        without = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None)
        with_projects = await tool.run_final_check(old_private, new_private, old_public, new_public, projects, None)

    assert without.counted == 0
    assert with_projects.counted == 1
    assert with_projects.still_opens_with_old[0].endswith("#repositories[0].password")


@pytest.mark.asyncio
async def test_the_final_check_walks_a_project_file_in_a_subdirectory(tmp_path: Path) -> None:
    """The fourth place is a tree, not a flat directory, and the verdict has to cover the tree.

    Both halves are the two readings of the same run: as the selection stands the nested file is
    counted and named as still opening with the old key, and flattened the same run reports CLEAN
    over a field the old key opens. That second half is the FIELD walk alone -- the inventory
    that would also catch it is emptied for every test here by ``no_coverage_sweep``, and has its
    own test below.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    projects = tmp_path / "projects"
    (projects / "local-old").mkdir(parents=True)
    await _project_file(projects, "hier", new_public)
    await _project_file(projects / "local-old", "oud", old_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
    ):
        walked = await tool.run_final_check(old_private, new_private, old_public, new_public, projects, None)
        with patch.object(tool, "project_files", lambda directory: sorted(Path(directory).glob("*.yaml"))):
            flat = await tool.run_final_check(old_private, new_private, old_public, new_public, projects, None)

    assert walked.counted == 2
    assert not walked.clean
    assert walked.still_opens_with_old == [f"{projects}/local-old/oud.yaml#repositories[0].password"]
    assert flat.counted == 1
    assert flat.clean


@pytest.mark.asyncio
async def test_the_final_check_names_a_projects_file_no_selection_reaches(tmp_path: Path) -> None:
    """The half of the projects verdict that does not come out of the selection it checks.

    A walk and a count built from the same glob agree with each other over everything that glob
    does not see. So the clone gets the same inventory this repo gets: git says which files the
    tree tracks, and any of them carrying real ciphertext that no selection reaches is a gap.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    projects = tmp_path / "projects"
    projects.mkdir()
    await _project_file(projects, "hier", new_public)
    stray = await _project_file(projects, "oud", new_public)
    stray = stray.rename(projects / "oud.yml")

    def only_the_projects_tree(tree: Path) -> dict[Path, int]:
        return files_with_ciphertext(tree) if tree == projects else {}

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
        patch.object(tool, "files_with_ciphertext", side_effect=only_the_projects_tree),
    ):
        check = await tool.run_final_check(old_private, new_private, old_public, new_public, projects, None)

    assert check.counted == 1
    assert check.still_opens_with_old == []
    assert check.outside_coverage == [str(stray)]
    assert not check.clean
    assert f"FAIL carries ciphertext and nothing converts it: {stray}" in check.lines()


@pytest.mark.asyncio
async def test_the_final_check_fails_when_the_count_does_not_match(tmp_path: Path) -> None:
    """All fields converted and readable, one fewer than the fingerprint: not clean."""
    old_private, _old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / ".env", {"A_PASSWORD": "x"}, new_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[path]),
    ):
        check = await tool.run_final_check(old_private, new_private, "age1a", "age1b", None, 2)

    assert check.still_opens_with_old == []
    assert check.does_not_open_with_new == []
    assert not check.count_matches
    assert not check.clean


def test_renaming_is_a_no_op_when_the_keys_already_sit_in_place(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The repo references security/key.txt everywhere, so the name is the migration.

    Running this against the real ``security/`` made it skip on every machine that has no keys
    there, which is every machine running CI -- so the no-op path was never measured. The fixed
    names are patched to a temporary pair instead, the way the two tests below do it.
    """
    target_old = tmp_path / "security" / "old_key.txt"
    target_new = tmp_path / "security" / "key.txt"
    target_old.parent.mkdir()
    target_old.write_text("old\n")
    target_new.write_text("new\n")

    with (
        patch.object(tool, "CANONICAL_OLD", target_old),
        patch.object(tool, "CANONICAL_NEW", target_new),
    ):
        tool.rename_keys(target_old, target_new, yes=True)

    assert "already sit under their fixed names" in capsys.readouterr().out
    assert (target_old.read_text(), target_new.read_text()) == ("old\n", "new\n")


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


def test_the_old_key_moves_aside_before_the_new_one_takes_the_fixed_name(tmp_path: Path) -> None:
    """The documented step 1 has the old key sitting on the name the new one is about to take.

    ``features/sops-sleutel-vervangen.md`` has the operator answer ``security/key.txt`` and
    ``security/nieuw.txt``, so the answered old key IS ``CANONICAL_NEW``. Move the new one first
    and it overwrites A before A has been copied anywhere. The three rename tests around this one
    never let the two paths overlap, so only this arrangement measures the order.
    """
    security = tmp_path / "security"
    security.mkdir()
    answered_old = security / "key.txt"
    answered_old.write_text("old\n")
    answered_new = security / "nieuw.txt"
    answered_new.write_text("new\n")

    with (
        patch.object(tool, "CANONICAL_OLD", security / "old_key.txt"),
        patch.object(tool, "CANONICAL_NEW", security / "key.txt"),
    ):
        tool.rename_keys(answered_old, answered_new, yes=True)

    assert (security / "old_key.txt").read_text() == "old\n"
    assert (security / "key.txt").read_text() == "new\n"
    assert not answered_new.exists()


@pytest.mark.asyncio
async def test_generating_the_new_key_makes_it_at_the_answered_path(tmp_path: Path) -> None:
    """Step 1 is one command: --rename makes the key too, instead of a hand-typed age-keygen.

    The new key is answered as a path that does NOT exist yet. Without the flag that answer is
    a missing file and the run stops at exit 2, which is what makes this test measure the
    generation rather than the rename it rides along with.
    """
    old_private, _old_public = generate_sops_key_pair()
    security = tmp_path / "security"
    security.mkdir()
    answered_old = security / "key.txt"
    answered_old.write_text(f"{old_private}\n")
    answered_new = security / "nieuw.txt"

    with (
        patch.object(tool, "CANONICAL_OLD", security / "old_key.txt"),
        patch.object(tool, "CANONICAL_NEW", security / "key.txt"),
        patch("builtins.input", side_effect=[str(answered_old), str(answered_new), "ja"]),
    ):
        code = await tool.main(["--rename", "--generate-new-key"])

    assert code == 0
    assert (security / "old_key.txt").read_text() == f"{old_private}\n"
    assert read_key(security / "key.txt") != old_private
    assert not answered_new.exists()


@pytest.mark.asyncio
async def test_generating_refuses_to_write_over_a_key_that_is_already_there(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The default answer for the new key IS the key in use, and that answer must not destroy it.

    ``age-keygen -o`` opens with O_EXCL and refuses this too, which is why the assert is on the
    WORDS: without the tool's own rule the run still ends at exit 2 with the path in it, and
    only the message tells "this is the key in use" from "the tool could not open a file".
    """
    old_private, _old_public = generate_sops_key_pair()
    security = tmp_path / "security"
    security.mkdir()
    answered_old = security / "key.txt"
    answered_old.write_text(f"{old_private}\n")

    with (
        patch.object(tool, "CANONICAL_OLD", security / "old_key.txt"),
        patch.object(tool, "CANONICAL_NEW", security / "key.txt"),
        patch("builtins.input", side_effect=[str(answered_old), str(answered_old)]),
    ):
        code = await tool.main(["--rename", "--generate-new-key"])

    assert code == 2
    error = capsys.readouterr().err
    assert "refusing to overwrite" in error
    assert str(answered_old) in error
    assert answered_old.read_text() == f"{old_private}\n"


@pytest.mark.asyncio
async def test_generating_a_key_without_the_rename_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Making the key and giving it the fixed name is one step, so the flags are one step too.

    The refusal comes before the first question, so nothing is written and nothing is asked.
    """
    would_be = tmp_path / "nieuw.txt"

    with patch("builtins.input", side_effect=AssertionError("must not ask")):
        code = await tool.main(["--generate-new-key", "--new-key", str(would_be)])

    assert code == 2
    assert "--rename" in capsys.readouterr().err
    assert not would_be.exists()


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
        patch.object(tool, "loose_paths", return_value=[]),
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
    new_private, new_public = generate_sops_key_pair()
    old_file = tmp_path / "old_key.txt"
    old_file.write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    projects = tmp_path / "projects"
    projects.mkdir()
    await _project_file(projects, "een", new_public)
    argo = tmp_path / "zad-argo-user-applications"
    argo.mkdir()

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
    ):
        code = await tool.main(
            [
                "--ja",
                "--remove-old-key",
                "--projects",
                str(projects),
                "--argo-applications",
                str(argo),
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


@pytest.mark.asyncio
async def test_the_documented_step_8_command_counts_the_projects_fingerprint_too(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The documented command is bare, so the projects fingerprint has to arrive by DEFAULT.

    Step 8 is ``--remove-old-key --projects <clone>/projects`` and names no fingerprint, so
    without a default the expected count covers this repo alone: a rotation where nothing is
    wrong ends on "count differs" and leaves the old key in place. Measured on the real repo:
    118 fields walked against 28 recorded.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    old_file = tmp_path / "old_key.txt"
    old_file.write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    env_path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "x"}, new_public)
    projects = tmp_path / "projects"
    projects.mkdir()
    argo = tmp_path / "zad-argo-user-applications"
    argo.mkdir()
    block = await encrypt_age_content("ghp_token", new_public)
    (projects / "een.yaml").write_text(
        "name: een\nrepositories:\n  - name: main-repo\n    password: "
        f"{BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}\n"
    )
    repo_fingerprint = tmp_path / "fingerprint.json"
    Fingerprint(fields={"this-repo#one-env-value": "1"}).save(repo_fingerprint)
    projects_fingerprint = tmp_path / "projects-fingerprint.json"
    command = [
        "--ja",
        "--remove-old-key",
        "--projects",
        str(projects),
        "--argo-applications",
        str(argo),
        "--old-key",
        str(old_file),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(repo_fingerprint),
    ]

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
        patch.object(tool, "DEFAULT_PROJECTS_FINGERPRINT", projects_fingerprint),
    ):
        # The count check really runs: with only this repo's fingerprint on disk the two fields
        # walked are one more than it recorded, and the old key stays.
        without = await tool.main(command)
        assert "FAIL count differs from the fingerprint: 2 now, 1 before" in capsys.readouterr().out
        Fingerprint(fields={f"{projects}/een.yaml#repositories[0].password": "2"}).save(projects_fingerprint)
        with_the_default = await tool.main(command)

    assert without == 1
    assert with_the_default == 0
    assert "CLEAN the old key opens nothing" in capsys.readouterr().out
    assert not old_file.exists()


def test_the_default_projects_fingerprint_is_the_file_the_projects_round_writes() -> None:
    """The two halves have to name the SAME file, or the default counts nothing.

    The test above patches the default to a temporary path, so it proves that the default is
    read and counted -- not that it points where ``rotate-project-keys.py`` puts its fingerprint.
    That agreement is the whole reason the documented bare command adds up.
    """
    assert REAL_PROJECTS_FINGERPRINT == KEY_FINGERPRINT


def test_every_documented_invocation_parses_and_the_final_check_walks_the_projects() -> None:
    """The feature doc is the operator's script, and nothing else reads it.

    Both final-check commands there are bare: no ``--projects-fingerprint``, so the count only
    adds up through the default, which the two tests above pin. And without ``--projects`` the
    fourth place is not walked and without ``--argo-applications`` the fifth, which makes
    ``--remove-old-key`` refuse on either. A flag that is renamed or dropped from the parser turns
    every line here into a SystemExit, instead of leaving a doc that has gone stale without
    anything saying so.

    This reads the FLAGS. Whether the command in front of them starts at all is a claim of its
    own, and ``test_every_documented_command_line_starts_as_written`` runs it.
    """
    documented = documented_lines("rotate-sops-key.py")
    final_checks = [line for line in documented if "--assert-old-key-dead" in line or "--remove-old-key" in line]

    assert len(documented) >= 5, "the documented run disappeared from the feature doc"
    assert len(final_checks) == 3, "the final check runs in VERIFY-1 and again in VERIFY-2, plus step 8"
    for line in documented:
        arguments = tool.build_parser().parse_args(flags(line))
        assert arguments.projects_fingerprint == str(tool.DEFAULT_PROJECTS_FINGERPRINT)
        if arguments.verify:
            # The fingerprint of step 2 covers both trees, so a --verify handed only this repo
            # reports every field of the argo clone as "field disappeared".
            assert arguments.argo_applications, f"--verify walks the argo clone too: {line}"
    for line in final_checks:
        arguments = tool.build_parser().parse_args(flags(line))
        assert arguments.projects, f"the final check walks the fourth place: {line}"
        assert arguments.argo_applications, f"the final check walks the fifth place: {line}"


#: The five entry points under ``scripts/``.
ENTRY_SCRIPTS = (
    "rotate-sops-key.py",
    "rotate-project-keys.py",
    "replace-git-pat.py",
    "set-sops-key-secret.py",
    "scan-secrets.py",
)

#: The documents that hand an operator a command line to paste.
COMMAND_DOCS = (
    tool.REPO / "features" / "sops-sleutel-vervangen.md",
    tool.REPO / "docs" / "geheimenscan-historie-2026-09-22.md",
    tool.REPO / "scripts" / "README.md",
)


def _pasteable_commands(doc: Path) -> list[str]:
    """Every line inside a ```bash block of ``doc`` that runs one of the entry points.

    Only inside a bash block: the same names appear in tables and in prose, and those are
    references and not commands. A line that starts with ``#`` is a comment in such a block.
    """
    commands = []
    inside = False
    for raw in doc.read_text().splitlines():
        if raw.startswith("```"):
            inside = raw.startswith("```bash")
            continue
        line = raw.strip()
        if inside and not line.startswith("#") and any(f"scripts/{name}" in line for name in ENTRY_SCRIPTS):
            commands.append(line)
    return commands


def _outside_the_venv() -> dict[str, str]:
    """The environment an operator has: no ``VIRTUAL_ENV``, and this venv's bin off ``PATH``.

    Pytest itself runs inside the OPI environment, so a ``python3`` started from here would
    resolve to the interpreter that already has pydantic, and a documented form that only works
    for us would read as green.
    """
    environment = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    venv_bin = str(Path(sys.prefix) / "bin")
    environment["PATH"] = os.pathsep.join(
        entry for entry in environment.get("PATH", "").split(os.pathsep) if entry != venv_bin
    )
    return environment


def _launcher_of(line: str) -> list[str]:
    """The tokens up to and including the script path: argv[0] and everything that carries it."""
    tokens = shlex.split(line)
    for index, token in enumerate(tokens):
        if token.startswith("scripts/") and token.endswith(".py"):
            return tokens[: index + 1]
    raise AssertionError(f"no script path in this line: {line}")


def test_every_documented_command_line_starts_as_written() -> None:
    """A documented command is a claim about argv[0] too, not only about its flags.

    Parsing the flags says nothing about whether the paste runs: that is decided by the launcher
    in front of them, and an operator runs these lines by hand, so nothing else would catch it.
    So this RUNS each unique invocation, with ``--help`` in place of the arguments, from the
    repository root the docs tell you to be in.

    It runs them in ``_outside_the_venv()``, which says why that matters.
    """
    commands = [line for doc in COMMAND_DOCS for line in _pasteable_commands(doc)]

    assert len(commands) >= 14, "the documented run disappeared from the docs"

    launchers = {tuple(_launcher_of(line)) for line in commands}
    scripts_covered = {launcher[-1].removeprefix("scripts/") for launcher in launchers}

    assert scripts_covered == set(ENTRY_SCRIPTS), "an entry point lost its documented invocation"

    for launcher in sorted(launchers):
        assert launcher[0] in {BARE_PYTHON, *shlex.split(LAUNCHER)[:1]}, f"unknown launcher: {launcher}"
        try:
            finished = subprocess.run(
                [*launcher, "--help"],
                cwd=tool.REPO,
                env=_outside_the_venv(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except OSError as refused:
            # A bare "scripts/x.py" without an exec bit: the shell would say 126 here, and
            # without a shell it is an OSError. Both mean the same thing for the operator.
            pytest.fail(f"{shlex.join(launcher)} does not start: {refused}")
        assert finished.returncode == 0, f"{shlex.join(launcher)} does not start: {finished.stderr[-400:]}"


def _documented_launchers() -> dict[str, set[str]]:
    """Per entry point, the launcher the docs put in front of it."""
    launchers: dict[str, set[str]] = {}
    for doc in COMMAND_DOCS:
        for line in _pasteable_commands(doc):
            tokens = _launcher_of(line)
            launchers.setdefault(tokens[-1].removeprefix("scripts/"), set()).add(shlex.join(tokens[:-1]))
    return launchers


def _committed_modes() -> dict[str, str]:
    """The file modes a fresh clone of this branch gets, which is what an operator runs."""
    listing = subprocess.run(
        ["git", "ls-files", "-s", "scripts/"],
        cwd=tool.REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    entries: dict[str, str] = {}
    for row in listing.stdout.splitlines():
        mode, _, remainder = row.partition(" ")
        entries[remainder.split("\t", 1)[1].removeprefix("scripts/")] = mode
    return entries


def test_a_shebang_only_sits_on_the_entry_a_bare_interpreter_can_finish() -> None:
    """A shebang plus an exec bit is a promise that ``./scripts/<entry>.py`` runs. Most cannot.

    Four of the five import ``opi`` through their module, so a bare interpreter gets as far as
    ``ModuleNotFoundError``, and a shebang on such a file invites exactly the invocation the
    launcher in the docs exists to replace. The one entry documented as ``python3 scripts/...``
    can keep that promise, so it carries both and is run here by its own path to prove it.

    The entry points are read from the tree and not from ``ENTRY_SCRIPTS``: a dash in the name is
    what makes a file an entry rather than an importable module (``scripts/README.md``), so a
    sixth one arriving cannot slip past this by not being in the list.
    """
    entries = {path.name for path in (tool.REPO / "scripts").glob("*-*.py")}

    assert entries == set(ENTRY_SCRIPTS), "an entry point under scripts/ is missing from ENTRY_SCRIPTS"

    launchers = _documented_launchers()
    modes = _committed_modes()

    for name in sorted(entries):
        assert launchers.get(name), f"scripts/{name} has no documented invocation to judge it by"
        assert len(launchers[name]) == 1, f"scripts/{name} is documented with two launchers: {launchers[name]}"
        shebang = (tool.REPO / "scripts" / name).read_text().startswith("#!")
        executable = modes[name] == "100755"

        if launchers[name] == {BARE_PYTHON}:
            assert shebang, f"scripts/{name} is documented as standalone but has no shebang"
            assert executable, f"scripts/{name} is documented as standalone but is committed as {modes[name]}"
            finished = subprocess.run(
                [f"./scripts/{name}", "--help"],
                cwd=tool.REPO,
                env=_outside_the_venv(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert finished.returncode == 0, f"./scripts/{name} does not start: {finished.stderr[-400:]}"
        else:
            assert not shebang, f"scripts/{name} needs {launchers[name]}, so a shebang on it is a dead end"
            assert not executable, f"scripts/{name} needs {launchers[name]}, so the exec bit on it is a dead end"


def test_the_scripts_hand_out_the_same_launcher_the_docs_do() -> None:
    """The tools print next steps and epilogs, and those are pasted exactly like a doc line.

    A next-step line is the one place where the wrong form is handed to an operator who has just
    finished a round and is not reading the README. Every ``scripts/<entry>.py`` in the sources
    is in command position -- the modules refer to each other by bare name -- so each one has to
    carry a launcher.
    """
    written = re.compile(r"(.{0,60})scripts/(" + "|".join(re.escape(name) for name in ENTRY_SCRIPTS) + ")")
    seen = 0
    for path in sorted((tool.REPO / "scripts").glob("*.py")):
        for before, name in written.findall(path.read_text()):
            seen += 1
            expected = BARE_PYTHON if name == "scan-secrets.py" else LAUNCHER
            assert before.endswith(f"{expected} "), f"{path.name} hands out a bare scripts/{name}: ...{before}"

    assert seen >= 15, "the scripts stopped naming each other, or the names changed"


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
        patch.object(tool, "loose_paths", return_value=[]),
        patch.object(tool, "OWN_PROJECTS", own),
    ):
        check = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None)

    assert check.counted == 1
    assert not check.clean
    assert check.still_opens_with_old[0].endswith("#repositories[0].password")


# ---------------------------------------------------------------------------
# the SOPS files themselves: the first of the five places, and the one the task is named after
# ---------------------------------------------------------------------------


SECRET_BODY = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: demo\nstringData:\n  password: hunter2\n"


@needs_sops
def test_sops_rotate_moves_the_file_to_the_new_recipient_and_mints_a_new_data_key(tmp_path: Path) -> None:
    """The plan's verify clause for the conversion: B opens it, A does not, ciphertext changed.

    The third assertion is the one that picks ``rotate`` over ``updatekeys``: that one leaves the
    data key in place, so a holder of A opens the file with the data key out of an older copy.
    The measurement is in features/sops-sleutel-vervangen.md.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = _sops_file(tmp_path, "demo", SECRET_BODY, old_public)
    ciphertext_before = _encrypted_values(path)
    assert ciphertext_before

    sops_rotate(path, old_public, new_public, old_private)

    assert sops_recipients(path) == [new_public]
    assert "hunter2" in (sops_plaintext(path, new_private) or "")
    assert sops_plaintext(path, old_private) is None
    assert _encrypted_values(path) != ciphertext_before


@needs_sops
def test_sops_rotate_names_the_file_and_leaves_it_alone_when_the_key_does_not_fit(tmp_path: Path) -> None:
    """A failure halfway through a rotation must not leave an unreadable file behind."""
    _old_private, old_public = generate_sops_key_pair()
    _new_private, new_public = generate_sops_key_pair()
    stranger_private, _stranger_public = generate_sops_key_pair()
    path = _sops_file(tmp_path, "demo", SECRET_BODY, old_public)
    before = path.read_text()

    with pytest.raises(ConversionFailed) as caught:
        sops_rotate(path, old_public, new_public, stranger_private)

    assert str(path) in str(caught.value)
    assert path.read_text() == before
    assert sops_recipients(path) == [old_public]


@pytest.mark.asyncio
@needs_sops
async def test_the_round_converts_a_sops_file_and_leaves_another_recipient_alone(tmp_path: Path) -> None:
    """Selection by recipient, measured through the whole round instead of on the selector.

    Measured on this tree right now: 21 SOPS files, all on the one platform recipient -- so a
    round that worked on "every sops file" would look correct here. It did not look correct
    before this branch, when ``sops-sandbox/`` held two files on a practice key, and it would not
    the next time a file arrives on the sandbox or developer key. The proof therefore has to be
    synthetic: a second recipient in the tree, untouched.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    _stranger_private, stranger_public = generate_sops_key_pair()
    ours = _sops_file(tmp_path, "platform", SECRET_BODY, old_public)
    theirs = _sops_file(tmp_path, "practice", SECRET_BODY, stranger_public)
    theirs_before = theirs.read_text()

    with _selecting_from(tmp_path):
        plan = await tool.build_plan([], old_private, new_private, old_public)
        assert plan.sops == [ours]
        await tool.run_rotation(plan, old_private, new_private, old_public, new_public)

    assert sops_recipients(ours) == [new_public]
    assert theirs.read_text() == theirs_before
    assert sops_recipients(theirs) == [stranger_public]


@pytest.mark.asyncio
@needs_sops
async def test_the_fingerprint_shows_a_rotated_sops_document_kept_its_content(tmp_path: Path) -> None:
    """The whole product of the tool: proof that nothing changed but the key.

    The document is hashed as one field, before with A and after with B, and the hash has to
    match -- a rotation that dropped or shifted a value inside the file fails here even though
    the file still decrypts.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = _sops_file(tmp_path, "demo", SECRET_BODY, old_public)

    with _selecting_from(tmp_path):
        before, closed_before = await tool.fingerprint_now([path], [], old_private, new_private)
        plan = await tool.build_plan([], old_private, new_private, old_public)
        await tool.run_rotation(plan, old_private, new_private, old_public, new_public)
        after, closed_after = await tool.fingerprint_now([path], [], new_private)

    assert closed_before == []
    assert closed_after == []
    assert list(before.fields) == [f"{path}#<sops-document>"]
    assert before.compare(after) == []
    assert "hunter2" not in path.read_text()


@pytest.mark.asyncio
@needs_sops
async def test_the_final_check_names_a_sops_file_that_was_skipped(tmp_path: Path) -> None:
    """A file left on A has to fail the check BY NAME, or the rotation is not demonstrably done.

    The check walks both recipients on purpose: a skipped file still sits on the old one, so
    walking only the new recipient would make it invisible and the check would come back clean.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    done = _sops_file(tmp_path, "converted", SECRET_BODY, old_public)
    forgotten = _sops_file(tmp_path, "forgotten", SECRET_BODY, old_public)
    sops_rotate(done, old_public, new_public, old_private)

    with _selecting_from(tmp_path), patch.object(tool, "loose_paths", return_value=[]):
        check = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None)

    assert check.counted == 2
    assert check.still_opens_with_old == [str(forgotten)]
    assert check.does_not_open_with_new == [str(forgotten)]
    assert not check.clean
    assert any(f"STILL opens with the old key: {forgotten}" in line for line in check.lines())


@pytest.mark.asyncio
@needs_sops
async def test_the_final_check_is_clean_once_every_sops_file_is_over(tmp_path: Path) -> None:
    """The other half: with nothing left on A the check says so, so a red one means something."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    for name in ("een", "twee"):
        sops_rotate(_sops_file(tmp_path, name, SECRET_BODY, old_public), old_public, new_public, old_private)

    with _selecting_from(tmp_path), patch.object(tool, "loose_paths", return_value=[]):
        check = await tool.run_final_check(old_private, new_private, old_public, new_public, None, 2)

    assert check.counted == 2
    assert check.clean
    assert "CLEAN the old key opens nothing, the new key opens everything" in check.lines()


# ---------------------------------------------------------------------------
# the entry point end to end, over both halves at once
# ---------------------------------------------------------------------------


async def _two_place_tree(tmp_path: Path, old_public: str) -> tuple[Path, Path, list[str]]:
    """A SOPS file and an env file on the same key, plus the two key files the tool asks for."""
    tree = tmp_path / "tree"
    tree.mkdir()
    sops_path = _sops_file(tree, "demo", SECRET_BODY, old_public)
    env_path = await _env_file(tree / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    return sops_path, env_path, [str(sops_path), str(env_path)]


def _key_files(tmp_path: Path, old_private: str, new_private: str) -> list[str]:
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    return ["--old-key", str(tmp_path / "old_key.txt"), "--new-key", str(tmp_path / "key.txt")]


@pytest.mark.asyncio
@needs_sops
async def test_a_dry_run_names_both_places_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """``--dry-run`` is what the operator looks at before the irreversible half starts."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    sops_path, env_path, _ = await _two_place_tree(tmp_path, old_public)
    before = (sops_path.read_text(), env_path.read_text())
    fingerprint = tmp_path / "fingerprint.json"

    with _selecting_from(sops_path.parent), patch.object(tool, "loose_paths", return_value=[env_path]):
        code = await tool.main(
            ["--ja", "--dry-run", *_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]
        )

    assert code == 0
    printed = capsys.readouterr().out
    assert "1 SOPS files on the old recipient" in printed
    assert "1 loose encrypted values on the old key" in printed
    assert "= 2 fields" in printed
    assert "Dry run: nothing was changed." in printed
    assert (sops_path.read_text(), env_path.read_text()) == before
    assert not fingerprint.exists()


@pytest.mark.asyncio
@needs_sops
async def test_the_whole_round_converts_both_places_and_leaves_a_checkable_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """One run of the tool as the operator runs it, over a SOPS file and a loose value at once."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    sops_path, env_path, _ = await _two_place_tree(tmp_path, old_public)
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]

    with _selecting_from(sops_path.parent), patch.object(tool, "loose_paths", return_value=[env_path]):
        code = await tool.main(["--ja", *arguments])
        capsys.readouterr()
        again = await tool.main(["--ja", *arguments])

    assert code == 0
    assert again == 0
    assert "Nothing to do: no field sits on the old key any more." in capsys.readouterr().out
    assert sops_recipients(sops_path) == [new_public]
    assert sops_plaintext(sops_path, old_private) is None
    assert not await opens_with(tool.all_loose_values([env_path])[0].value, old_private)

    written = fingerprint.read_text()
    assert len(Fingerprint.load(fingerprint).fields) == 2
    assert "hunter2" not in written
    assert "AGE-SECRET-KEY-" not in written


async def _two_tree_round(tmp_path: Path, old_public: str) -> tuple[Path, Path, Path]:
    """This repo with one loose value, and an argo clone with one SOPS file on the same key.

    The shape of the two-run path the documentation describes: step 2 can be run without
    ``--argo-applications`` and then again with it, so the second run is a converting run that
    meets a record the first one wrote.
    """
    repo = tmp_path / "rig-cluster"
    clone = tmp_path / "zad-argo-user-applications"
    repo.mkdir()
    clone.mkdir()
    env_path = await _env_file(repo / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    _sops_file(clone, "argo-repository-main-repo", SECRET_BODY, old_public)
    return repo, clone, env_path


def _argo_sops_file(clone: Path) -> Path:
    """The one file the argo clone of ``_two_tree_round`` holds."""
    return clone / "argo-repository-main-repo.sops.yaml"


@pytest.mark.asyncio
@needs_sops
async def test_a_second_converting_run_refuses_to_overwrite_the_record_it_disagrees_with(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A changed plaintext between two converting runs, and the record is the only witness.

    This is the same failure the projects round has a guard for, on the place that round does not
    touch. Writing the record without comparing it makes the second run's hash the truth in
    silence: ``--verify`` then says CLEAN, ``--assert-old-key-dead`` says the old key is dead, and
    ``--remove-old-key`` acts on that verdict -- all four reading a record the run that changed
    the value had just rewritten. The earlier record has to survive, because it is the evidence.

    And the round has to stop BEFORE it converts, not report afterwards. A guard that runs once
    the files are on the new key leaves the collection converted under a record that was refused:
    the old key still opens nothing, so the final check calls it CLEAN, and the one measurement
    that disagreed is the one that was thrown away.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    repo, clone, env_path = await _two_tree_round(tmp_path, old_public)
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]

    with patch.object(tool, "REPO", repo), patch.object(tool, "loose_paths", return_value=[env_path]):
        assert await tool.main(["--ja", *arguments]) == 0
        recorded = fingerprint.read_text()
        capsys.readouterr()

        # Perfectly readable with the new key, so no decryption test can see this. Only the record can.
        env_path.write_text(f"GIT_PROJECTS_SERVER_PASSWORD={await _base64_value('something-else', new_public)}\n")
        untouched = _argo_sops_file(clone).read_text()
        code = await tool.main(["--ja", *arguments, "--argo-applications", str(clone)])

    printed = capsys.readouterr().out
    assert code == 1
    assert "disagrees with what is there now" in printed
    assert f"content changed: {env_path}#GIT_PROJECTS_SERVER_PASSWORD" in printed
    assert fingerprint.read_text() == recorded, "the earlier record is the evidence and has to stand"
    assert _argo_sops_file(clone).read_text() == untouched, "the refusal comes before a single file is converted"


@pytest.mark.asyncio
@needs_sops
async def test_a_place_left_out_of_the_first_run_joins_the_record_on_the_second(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The other side of that guard, and the reason it compares shared names only.

    Leaving ``--argo-applications`` off is allowed, so its fields are simply new to the record
    when the flag does come along. The run that RECORDS is the one that decides which fields end
    up in there, so that is where the note about the missing flag belongs -- the final check's
    note comes too late to change anything.

    Both stands, because only the silence pins the condition: a note that prints whether or not
    the flag was given says the record is short of the ArgoCD secrets in the very run that put
    them in it, right above the plan the operator says yes to.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    repo, clone, env_path = await _two_tree_round(tmp_path, old_public)
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]

    with patch.object(tool, "REPO", repo), patch.object(tool, "loose_paths", return_value=[env_path]):
        assert await tool.main(["--ja", *arguments]) == 0
        first = capsys.readouterr().out
        code = await tool.main(["--ja", *arguments, "--argo-applications", str(clone)])

    printed = capsys.readouterr().out
    note = "NOTE without --argo-applications this run leaves the ArgoCD repository secrets"
    assert note in first
    assert note not in printed, "the second run walked the clone, so it has nothing to leave out"
    assert code == 0
    assert "disagrees with what is there now" not in printed
    assert "new in the collection since the last round" in printed
    assert sorted(Fingerprint.load(fingerprint).fields) == [
        f"{env_path}#GIT_PROJECTS_SERVER_PASSWORD",
        f"{clone}/argo-repository-main-repo.sops.yaml#<sops-document>",
    ]


@pytest.mark.asyncio
async def test_a_full_round_converts_a_python_literal_and_a_whole_file_block(tmp_path: Path) -> None:
    """The two shapes the worklist used to walk past, through the tool as the operator runs it.

    Both halves matter and they fail differently. A Python literal has to keep the line it sits
    on -- the quotes, the type annotation, the lines around it -- because the file is source code
    that still has to import. A whole-file block has no line to anchor to at all and is rewritten
    entire, and it has to come back as an armored block: a value that switched storage form is
    itself a change, and nothing downstream would read it.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    source = tmp_path / "config.py"
    source.write_text(
        "class Settings:\n"
        '    PROJECT_REPO_USERNAME: str = "git"\n'
        f'    PROJECT_REPO_PASSWORD: str = "{await _base64_value("hunter2", old_public)}"\n'
        '    PROJECT_REPO_BRANCH: str = "main"\n'
    )
    whole = tmp_path / "age-secret-github.txt"
    whole.write_text(f"{await encrypt_age_content('ghp_a-second-token', old_public)}\n")

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[source, whole]),
    ):
        code = await tool.main(
            [
                "--ja",
                *_key_files(tmp_path, old_private, new_private),
                "--fingerprint",
                str(tmp_path / "fingerprint.json"),
            ]
        )

    assert code == 0
    lines = source.read_text().splitlines()
    assert lines[1] == '    PROJECT_REPO_USERNAME: str = "git"'
    assert lines[2].startswith('    PROJECT_REPO_PASSWORD: str = "base64+age:')
    assert lines[2].endswith('"')
    assert lines[3] == '    PROJECT_REPO_BRANCH: str = "main"'

    converted = tool.all_loose_values([source, whole])
    assert [field_.name for field_ in converted] == ["PROJECT_REPO_PASSWORD", "<file>"]
    assert [await decrypt_field(field_.value, new_private) for field_ in converted] == [
        "hunter2",
        "ghp_a-second-token",
    ]
    assert [await opens_with(field_.value, old_private) for field_ in converted] == [False, False]
    assert whole.read_text().startswith("-----BEGIN AGE ENCRYPTED FILE-----")


@pytest.mark.asyncio
@needs_sops
async def test_verify_says_clean_long_after_the_round_and_red_when_a_file_drifted(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--verify`` has to stand on its own, months later, without converting anything.

    Second half: a file put back on the old key must make it red. Without that this mode could
    report CLEAN on a tree it can no longer open.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    sops_path, env_path, _ = await _two_place_tree(tmp_path, old_public)
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]

    with _selecting_from(sops_path.parent), patch.object(tool, "loose_paths", return_value=[env_path]):
        assert await tool.main(["--ja", *arguments]) == 0
        capsys.readouterr()

        assert await tool.main(["--ja", "--verify", *arguments]) == 0
        assert "CLEAN 2 fields readable with the new key and unchanged in content" in capsys.readouterr().out

        # Put the SOPS file back on the old key: the same content, but the new key cannot read it.
        sops_rotate(sops_path, new_public, old_public, new_private)
        assert await tool.main(["--ja", "--verify", *arguments]) == 1

    assert f"FAIL does not open with the new key: {sops_path}#<sops-document>" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_verify_still_stands_once_the_old_key_file_has_been_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Step 8 deletes ``security/old_key.txt``, and ``--verify`` has to survive that.

    Plan and documentation both promise this check still runs months later. Demanding the file
    the previous step removed turned that promise into exit 2 "file does not exist".

    Second half: the relaxation is for ``--verify`` and nothing else. The final check MEASURES
    with the old key, so there it stays a hard requirement.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    env_path = await _env_file(
        tmp_path / ".env",
        {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2", "GIT_ARGO_APPLICATIONS_PASSWORD": "x"},
        old_public,
    )
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
    ):
        assert await tool.main(["--ja", *arguments]) == 0
        (tmp_path / "old_key.txt").unlink()
        capsys.readouterr()

        code = await tool.main(["--ja", "--verify", *arguments])
        printed = capsys.readouterr().out
        still_demanded = await tool.main(["--ja", "--assert-old-key-dead", *arguments])

    assert code == 0
    assert f"NOTE file does not exist: {tmp_path / 'old_key.txt'}" in printed
    assert "CLEAN 2 fields readable with the new key and unchanged in content" in printed
    assert still_demanded == 2
    assert "file does not exist" in capsys.readouterr().err


@pytest.mark.asyncio
@needs_sops
async def test_verify_without_the_old_key_still_names_a_file_that_stayed_on_the_old_recipient(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Narrowing the SELECTION must not narrow what the check catches.

    Without the old key the walk covers the new recipient alone, so a file that the round
    skipped falls outside it. What keeps it visible is the fingerprint: it recorded the field,
    and a field that cannot be measured reads as gone. Without that, the one stand meant to be
    runnable months later would call a half-finished rotation clean.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    tree = tmp_path / "tree"
    tree.mkdir()
    _sops_file(tree, "converted", "value: hunter2\n", old_public)
    skipped = _sops_file(tree, "skipped", "value: also-secret\n", old_public)
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]

    with _selecting_from(tree), patch.object(tool, "loose_paths", return_value=[]):
        assert await tool.main(["--ja", *arguments]) == 0
        assert len(Fingerprint.load(fingerprint).fields) == 2
        # the file drifts back onto the old recipient: what a skipped file looks like afterwards
        sops_rotate(skipped, new_public, old_public, new_private)
        capsys.readouterr()

        with_the_old_key = await tool.main(["--ja", "--verify", *arguments])
        (tmp_path / "old_key.txt").unlink()
        without_the_old_key = await tool.main(["--ja", "--verify", *arguments])

    printed = capsys.readouterr().out
    assert with_the_old_key == 1
    assert without_the_old_key == 1
    assert f"FAIL does not open with the new key: {skipped}#<sops-document>" in printed
    assert f"FAIL field disappeared: {skipped}#<sops-document>" in printed


@pytest.mark.asyncio
@needs_sops
async def test_verify_is_red_on_a_file_that_is_not_in_the_fingerprint_and_opens_with_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A field the fingerprint never saw AND the new key cannot open must not read as clean.

    The hash comparison cannot catch this one: the field is in neither measurement, so it is
    neither "disappeared" nor "changed". Only the list of fields that opened with no key at all
    carries it, which is why that list has its own say in the exit code -- otherwise this run
    prints a FAIL line and then reports CLEAN with a zero exit.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    sops_path, env_path, _ = await _two_place_tree(tmp_path, old_public)
    fingerprint = tmp_path / "fingerprint.json"
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]

    with _selecting_from(sops_path.parent), patch.object(tool, "loose_paths", return_value=[env_path]):
        assert await tool.main(["--ja", *arguments]) == 0
        # A file that arrives AFTER the fingerprint was taken and was never converted: it is
        # walked (the old recipient is part of the selection) but the new key does not open it.
        stray = _sops_file(sops_path.parent, "stray", SECRET_BODY, old_public)
        capsys.readouterr()

        code = await tool.main(["--ja", "--verify", *arguments])

    printed = capsys.readouterr().out
    assert code == 1
    assert f"FAIL does not open with the new key: {stray}#<sops-document>" in printed
    assert "CLEAN" not in printed


# ---------------------------------------------------------------------------
# the worklist itself
# ---------------------------------------------------------------------------


async def _set_repository_password(path: Path, plaintext: str, public_key: str) -> None:
    """Give an existing project file a repository password with other content behind it."""
    block = await encrypt_age_content(plaintext, public_key)
    encoded = base64.b64encode(block.encode()).decode()
    text = path.read_text()
    path.write_text(text.replace(text.split("password: ")[1].split("\n")[0], f"{BASE64_AGE_PREFIX}{encoded}"))


@pytest.mark.asyncio
async def test_verify_checks_the_projects_record_it_was_handed(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """``--verify`` judged ``--projects`` strictly and then walked nothing with it.

    A missing or empty directory is exit 2 in this mode, which reads as a flag that is going to
    be used. It was not: measured after a full round, the verdict said "CLEAN 33 fields" -- this
    repo's own -- while ``security/projects-fingerprint.json`` sat next to it with 106 fields
    that nothing read. Two halves, so two assertions: the projects fields have to be COUNTED,
    and a plaintext that has drifted since the round has to make this red.

    The drifted value is re-encrypted for the NEW key, so "does it open" cannot catch it. Only
    the recorded hash can.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    env_path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    clone = (tmp_path / "zad-projects" / "projects").resolve()
    clone.mkdir(parents=True)
    project = await _project_file(clone, "een", new_public)
    fingerprint = tmp_path / "fingerprint.json"
    projects_fingerprint = tmp_path / "projects-fingerprint.json"
    keys = _key_files(tmp_path, old_private, new_private)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
    ):
        assert await tool.main(["--ja", *keys, "--fingerprint", str(fingerprint)]) == 0
        # The projects round owns its own record; what is measured here is whether --verify reads it.
        await save_fingerprint(clone, str(projects_fingerprint), old_private, new_private)
        capsys.readouterr()
        verify = [
            "--ja",
            "--verify",
            *keys,
            "--fingerprint",
            str(fingerprint),
            "--projects",
            str(clone),
            "--projects-fingerprint",
            str(projects_fingerprint),
        ]

        assert await tool.main(verify) == 0
        clean = capsys.readouterr().out
        await _set_repository_password(project, "ghp_somebody_elses_token", new_public)
        drifted = await tool.main(verify)

    printed = capsys.readouterr().out
    assert "CLEAN 2 fields readable with the new key and unchanged in content" in clean, (
        "the one loose value plus the one project field, or the projects record is not counted"
    )
    assert drifted == 1
    assert f"FAIL content changed: {project}#repositories[0].password" in printed


@pytest.mark.asyncio
async def test_verify_refuses_projects_without_the_record_to_check_it_against(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Handing this mode a clone and no record is the one case it cannot answer.

    Checking this repo alone and printing CLEAN would be the same silence the flag already had.
    Walking the clone without a record to compare against would be a decryption test wearing the
    word "unchanged", which is what ``--assert-old-key-dead`` is for.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    env_path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    clone = tmp_path / "zad-projects" / "projects"
    clone.mkdir(parents=True)
    await _project_file(clone, "een", old_public)
    fingerprint = tmp_path / "fingerprint.json"
    absent = tmp_path / "projects-fingerprint.json"
    keys = _key_files(tmp_path, old_private, new_private)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
    ):
        assert await tool.main(["--ja", *keys, "--fingerprint", str(fingerprint)]) == 0
        capsys.readouterr()
        code = await tool.main(
            [
                "--ja",
                "--verify",
                *keys,
                "--fingerprint",
                str(fingerprint),
                "--projects",
                str(clone),
                "--projects-fingerprint",
                str(absent),
            ]
        )

    printed = capsys.readouterr()
    assert code == 2
    assert f"FAIL no fingerprint to check --projects against: {absent}" in printed.err
    assert "CLEAN" not in printed.out


def test_the_coverage_sweep_walks_every_tree_of_the_run_and_not_this_repo_alone(tmp_path: Path) -> None:
    """The sweep ran here and over the projects clone. The argo clone was reasoned about.

    ``--remove-old-key`` demands ``--argo-applications`` and then walks that clone for its SOPS
    files and nothing else, so a tracked file there carrying a loose value was reached by no
    place and counted by no fingerprint: the verdict said CLEAN and the old key went away on it.
    ``argo_manager.py`` writing only SOPS files is what made that safe in practice, and a claim
    about another module's behaviour is exactly what this guard exists to stop repeating.

    What covers a file in such a clone is ``sops_files()`` -- SOPS metadata, whatever the file is
    called -- so the second half is that a file which HAS that metadata is not a gap.
    """
    clone = tmp_path / "zad-argo"
    (clone / "apps" / "prj-a").mkdir(parents=True)
    loose = clone / "apps" / "prj-a" / "los.yaml"
    loose.write_text("password: base64+age:whatever\n")
    sops_owned = clone / "apps" / "prj-a" / "repo.sops.yaml"
    sops_owned.write_text("password: base64+age:whatever\n")
    inventory = {loose: 1, sops_owned: 1}

    with patch.object(tool, "files_with_ciphertext", side_effect=lambda tree: inventory if tree == clone else {}):
        assert tool.coverage_gaps() == [], "this repo's own sweep must not change"
        assert tool.coverage_gaps([tool.REPO, clone]) == [loose, sops_owned]

        sops_owned.write_text("password: base64+age:whatever\nsops:\n    age: []\n")
        assert tool.coverage_gaps([tool.REPO, clone]) == [loose]


@pytest.mark.asyncio
async def test_the_final_check_refuses_to_call_the_old_key_dead_over_a_gap_in_the_argo_clone(tmp_path: Path) -> None:
    """The same verdict, through the check the operator actually runs before deleting the key."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    clone = tmp_path / "zad-argo"
    clone.mkdir()
    forgotten = clone / "los.yaml"

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
        patch.object(tool, "files_with_ciphertext", side_effect=lambda tree: {forgotten: 1} if tree == clone else {}),
    ):
        clean = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None, [tool.REPO])
        gap = await tool.run_final_check(
            old_private, new_private, old_public, new_public, None, None, [tool.REPO, clone]
        )

    assert clean.clean, "without the clone there is nothing to find, which is the old blind spot"
    assert not gap.clean
    assert gap.outside_coverage == [str(forgotten)]


@pytest.mark.asyncio
async def test_a_gap_in_another_tree_stops_the_round_before_a_byte_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The sweep over every tree has a second reader, and it is the one that comes first.

    ``plan.gaps`` stops the rotation, and that is the moment the verdict is still cheap: the
    final check refusing afterwards means the old key is already gone from the files the round
    did convert. ``test_a_coverage_gap_stops_the_round_before_a_byte_is_written`` measures that
    stop for this repo's own tree; this one measures it for a tree handed in on the command
    line, which is the half the sweep gained and the half with no reader before it.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    env_path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    before = env_path.read_text()
    clone = tmp_path / "zad-argo"
    clone.mkdir()
    forgotten = clone / "los.yaml"

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
        patch.object(tool, "files_with_ciphertext", side_effect=lambda tree: {forgotten: 1} if tree == clone else {}),
    ):
        code = await tool.main(
            [
                "--ja",
                *_key_files(tmp_path, old_private, new_private),
                "--fingerprint",
                str(tmp_path / "fingerprint.json"),
                "--argo-applications",
                str(clone),
            ]
        )

    printed = capsys.readouterr()
    assert code == 1
    assert "STOPPED a tracked file carries ciphertext that nothing here converts." in printed.err
    assert f"FAIL carries ciphertext and nothing converts it: {forgotten}" in printed.out
    assert env_path.read_text() == before, "the round may not convert anything while a place is unaccounted for"


@pytest.mark.asyncio
async def test_verify_is_red_on_a_project_field_that_opens_with_neither_key(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A field the record does not know cannot be caught by comparing the record.

    The comparison answers for every field that was there when the round ran. A project file
    added since is in neither record nor objection -- it opens with no key at all, so it is not
    in what was measured either, and nothing would say a word about it. That is what the closed
    list is for on this repo's own fields, and the projects clone has the same one.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    _third_private, third_public = generate_sops_key_pair()
    env_path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    clone = (tmp_path / "zad-projects" / "projects").resolve()
    clone.mkdir(parents=True)
    await _project_file(clone, "een", new_public)
    fingerprint = tmp_path / "fingerprint.json"
    projects_fingerprint = tmp_path / "projects-fingerprint.json"
    keys = _key_files(tmp_path, old_private, new_private)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
    ):
        assert await tool.main(["--ja", *keys, "--fingerprint", str(fingerprint)]) == 0
        await save_fingerprint(clone, str(projects_fingerprint), old_private, new_private)
        capsys.readouterr()
        # Added after the record was written, and on a key nobody involved here holds.
        stranger = await _project_file(clone, "twee", third_public)
        code = await tool.main(
            [
                "--ja",
                "--verify",
                *keys,
                "--fingerprint",
                str(fingerprint),
                "--projects",
                str(clone),
                "--projects-fingerprint",
                str(projects_fingerprint),
            ]
        )

    printed = capsys.readouterr().out
    assert code == 1
    assert f"FAIL does not open with the new key: {stranger}#repositories[0].password" in printed
    assert "CLEAN" not in printed


@pytest.mark.asyncio
async def test_verify_matches_the_projects_record_however_the_clone_was_spelled(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The other side of the resolved path, and the side that does the comparing.

    ``rotate-project-keys.py`` records every field under the resolved path of its file. This
    mode compares those NAMES, so a clone handed to it with a ``..`` in it -- which is what a
    documented ``<clone>/projects`` next to a relative path produces -- would read as a
    collection in which every field disappeared and another one appeared, over a rotation where
    nothing at all is wrong.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    env_path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    clone = (tmp_path / "zad-projects" / "projects").resolve()
    clone.mkdir(parents=True)
    await _project_file(clone, "een", new_public)
    fingerprint = tmp_path / "fingerprint.json"
    projects_fingerprint = tmp_path / "projects-fingerprint.json"
    keys = _key_files(tmp_path, old_private, new_private)
    detour = clone / ".." / "projects"

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
    ):
        assert await tool.main(["--ja", *keys, "--fingerprint", str(fingerprint)]) == 0
        await save_fingerprint(clone, str(projects_fingerprint), old_private, new_private)
        capsys.readouterr()
        code = await tool.main(
            [
                "--ja",
                "--verify",
                *keys,
                "--fingerprint",
                str(fingerprint),
                "--projects",
                str(detour),
                "--projects-fingerprint",
                str(projects_fingerprint),
            ]
        )

    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "CLEAN 2 fields readable with the new key and unchanged in content" in printed


def test_every_configured_loose_value_file_is_really_there_and_holds_an_encrypted_value() -> None:
    """The row that gets forgotten: ``sops rotate`` does not see a loose ``base64+age:`` value.

    ``loose_paths()`` drops a path that does not exist, so a moved or renamed configmap costs the
    tool a file WITHOUT any complaint -- the round would pass, the final check would pass, and
    that value would stay on the old key. This is the assertion that turns such a move into a
    red test instead of a silent miss.
    """
    missing = [name for name in tool.LOOSE_VALUE_FILES if not (tool.REPO / name).is_file()]

    assert missing == []
    assert "operations-manager/python/.env" in tool.LOOSE_VALUE_FILES
    for path in tool.loose_paths():
        assert loose_values(path), f"{path} carries no base64+age: value any more"


def test_the_feature_doc_counts_what_the_worklist_really_holds() -> None:
    """The doc row that tells an operator what this entry covers, against the inventory itself.

    That row said eight loose values while the tool printed nine, and a hand count was the only
    thing that found it. A number in the doc is a claim about the worklist, so a file arriving in
    ``LOOSE_VALUE_FILES`` or a new SOPS file in this tree has to be visible here.
    """
    rows = [
        line
        for line in (tool.REPO / "features" / "sops-sleutel-vervangen.md").read_text().splitlines()
        if line.startswith("| `scripts/rotate-sops-key.py`")
    ]

    assert len(rows) == 1, "the row that says what this entry covers is gone from the feature doc"

    row = rows[0]
    counted = {word: int(number) for number, word in re.findall(r"(\d+) (SOPS-bestanden|losse waarden)", row)}

    assert set(counted) == {"SOPS-bestanden", "losse waarden"}, f"this row no longer counts both: {row}"

    # Counted into locals first: a bare len(...) in the assert makes pytest print every
    # LooseValue it found, ciphertext and all, over a number that is off by one.
    sops_count = len(sops_files(tool.REPO))
    loose_count = len(tool.all_loose_values(tool.loose_paths()))

    assert counted["SOPS-bestanden"] == sops_count, f"update this row: {row}"
    assert counted["losse waarden"] == loose_count, f"update this row: {row}"


def test_the_three_values_a_review_found_outside_the_worklist_are_in_it() -> None:
    """Named one by one, because each one is a different SHAPE the env pattern walked past."""
    found = {
        str(field_.path.relative_to(tool.REPO)): (field_.name, field_.line_number)
        for path in tool.loose_paths()
        for field_ in loose_values(path)
    }

    assert found["operations-manager/python/opi/core/config.py"] == ("PROJECT_REPO_PASSWORD", 238)
    assert found["operations-manager/python/scripts/migrate_project_to_production.py"] == ("password", 66)
    assert found["projects/age-secret-github.txt"] == ("<file>", None)


def test_nothing_in_this_tree_carries_ciphertext_that_no_place_converts() -> None:
    """The guard, measured against the real tree: this is what the worklist was missing.

    The two inner patches put the real tree back: ``no_coverage_sweep`` empties the inventory and
    ``no_own_projects`` points ``projects/`` elsewhere for every other test here, and this is the
    one test that is about what the repo really holds.
    """
    with (
        patch.object(tool, "files_with_ciphertext", files_with_ciphertext),
        patch.object(tool, "OWN_PROJECTS", tool.REPO / "projects"),
    ):
        assert tool.coverage_gaps() == []


@pytest.mark.asyncio
@needs_sops
async def test_the_round_also_converts_the_argocd_repository_secrets(tmp_path: Path) -> None:
    """The fifth place, and it is in another repo: zad-argo-user-applications.

    Why those secrets hang off the platform key is in ``sops_trees()``. Selection stays on the
    recipient inside that clone: a file there on another key is not touched, which is what the
    second SOPS file measures.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    other_private, other_public = generate_sops_key_pair()
    clone = tmp_path / "zad-argo-user-applications"
    clone.mkdir()
    ours = _sops_file(clone, "argo-repository-main-repo", SECRET_BODY, old_public)
    theirs = _sops_file(clone, "someone-elses", SECRET_BODY, other_public)

    with patch.object(tool, "loose_paths", return_value=[]), patch.object(tool, "REPO", tmp_path / "empty-repo"):
        code = await tool.main(
            [
                "--ja",
                *_key_files(tmp_path, old_private, new_private),
                "--fingerprint",
                str(tmp_path / "fingerprint.json"),
                "--argo-applications",
                str(clone),
            ]
        )

    assert code == 0
    assert sops_recipients(ours) == [new_public]
    assert sops_plaintext(ours, old_private) is None
    assert sops_plaintext(ours, new_private) is not None
    assert sops_recipients(theirs) == [other_public]
    assert sops_plaintext(theirs, other_private) is not None


@pytest.mark.asyncio
@needs_sops
async def test_the_final_check_walks_this_repo_and_the_argo_clone_in_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The verdict is only as wide as the trees it is handed, and one of them is another repo.

    Both halves are measured in one run, because dropping either is the same failure. A file
    left behind HERE and one left behind in the argo clone both have to be named: walk only
    this repo and the ArgoCD repository secrets go quiet, walk only the clone and this repo's
    SOPS files do -- and either way ``--assert-old-key-dead`` calls the old key dead over a
    place it never opened, which is the one verdict the whole cutover hangs on.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    repo = tmp_path / "rig-cluster"
    clone = tmp_path / "zad-argo-user-applications"
    repo.mkdir()
    clone.mkdir()
    here = _sops_file(repo, "operations-manager-env-secrets", SECRET_BODY, old_public)
    there = _sops_file(clone, "argo-repository-main-repo", SECRET_BODY, old_public)

    with patch.object(tool, "REPO", repo), patch.object(tool, "loose_paths", return_value=[]):
        code = await tool.main(
            [
                "--ja",
                "--assert-old-key-dead",
                *_key_files(tmp_path, old_private, new_private),
                "--argo-applications",
                str(clone),
            ]
        )

    printed = capsys.readouterr().out
    assert code == 1
    assert f"FAIL STILL opens with the old key: {here}" in printed
    assert f"FAIL STILL opens with the old key: {there}" in printed


@pytest.mark.asyncio
async def test_the_final_check_names_the_places_its_flags_left_out_and_stays_quiet_about_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """CLEAN over four places reads exactly like CLEAN over five, so the note carries the width.

    ``--remove-old-key`` deletes the old key on this same verdict, and a run without a flag
    reports CLEAN over a place it never opened. The silence is the half that is easy to lose:
    a note that stands whichever flags were given tells a complete run that it was incomplete,
    and is one nobody reads any more.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    keys = _key_files(tmp_path, old_private, new_private)
    projects = tmp_path / "projects"
    projects.mkdir()
    await _project_file(projects, "een", new_public)
    argo = tmp_path / "zad-argo-user-applications"
    argo.mkdir()
    records = ["--fingerprint", str(tmp_path / "absent.json"), "--projects-fingerprint", str(tmp_path / "absent2.json")]
    stands = {
        "neither": [],
        "only --projects": ["--projects", str(projects)],
        "only --argo-applications": ["--argo-applications", str(argo)],
        "both": ["--projects", str(projects), "--argo-applications", str(argo)],
    }

    printed: dict[str, str] = {}
    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
        patch.object(tool, "OWN_PROJECTS", tmp_path / "not-a-directory"),
    ):
        for stand, given in stands.items():
            assert await tool.main(["--ja", "--assert-old-key-dead", *keys, *records, *given]) == 0, stand
            printed[stand] = capsys.readouterr().out

    projects_note = "NOTE without --projects the final check does not walk the fourth place."
    argo_note = "NOTE without --argo-applications the final check does not walk the ArgoCD"
    for stand, given in stands.items():
        assert (projects_note in printed[stand]) is ("--projects" not in given), f"the fourth place, {stand}"
        assert (argo_note in printed[stand]) is ("--argo-applications" not in given), f"the ArgoCD secrets, {stand}"


@pytest.mark.asyncio
async def test_a_clone_path_that_is_not_there_stops_the_run_instead_of_walking_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A typo in the clone path must not read as "the fifth place holds nothing".

    ``sops_files_for`` on a directory that is not there answers with an empty list and no
    complaint, so without this refusal the run walks four places, finds them clean and prints
    CLEAN -- while the ArgoCD repository secrets still sit on the old key.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    absent = tmp_path / "zad-argo-typo"

    with patch.object(tool, "sops_files_for", return_value=[]), patch.object(tool, "loose_paths", return_value=[]):
        code = await tool.main(
            [
                "--ja",
                "--assert-old-key-dead",
                *_key_files(tmp_path, old_private, new_private),
                "--argo-applications",
                str(absent),
            ]
        )

    assert code == 2
    assert f"FAIL no such directory: {absent}" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_projects_is_refused_when_it_holds_no_project_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The mirror of the refusal ``--argo-applications`` already had, on both shapes.

    A path that does not exist was refused; a path that exists and holds no project file was
    not, and that one is the sharper of the two -- it walks the fourth place over nothing and
    reports CLEAN, which is the answer the operator deletes the old key on.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    clone = tmp_path / "zad-projects"
    (clone / "projects").mkdir(parents=True)
    keys = _key_files(tmp_path, old_private, new_private)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
    ):
        absent = await tool.main(["--ja", "--assert-old-key-dead", *keys, "--projects", str(tmp_path / "typo")])
        empty = await tool.main(["--ja", "--assert-old-key-dead", *keys, "--projects", str(clone)])

    printed = capsys.readouterr().err
    assert absent == 2
    assert empty == 2
    assert f"FAIL no such directory: {tmp_path / 'typo'}" in printed
    assert f"FAIL no project files under {clone}" in printed
    assert "Check the path: the documented one is <clone>/projects." in printed


@pytest.mark.asyncio
@needs_sops
async def test_verify_measures_the_argo_clone_too_and_says_so_when_it_is_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--verify`` runs months later, and it has to cover the same trees the round did.

    The fingerprint of the round holds this repo AND the argo clone, so a ``--verify`` handed
    only one of them reports every field of the other as gone. Both halves are here because
    they are the two ways to read the same command: with the clone it is CLEAN, without it the
    argo field is "field disappeared" -- and that second half is exactly what the documented
    command avoids by carrying the flag.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    repo = tmp_path / "rig-cluster"
    clone = tmp_path / "zad-argo-user-applications"
    repo.mkdir()
    clone.mkdir()
    _sops_file(repo, "demo", SECRET_BODY, old_public)
    there = _sops_file(clone, "argo-repository-main-repo", SECRET_BODY, old_public)
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(tmp_path / "fingerprint.json")]

    with patch.object(tool, "REPO", repo), patch.object(tool, "loose_paths", return_value=[]):
        assert await tool.main(["--ja", *arguments, "--argo-applications", str(clone)]) == 0
        capsys.readouterr()

        assert await tool.main(["--ja", "--verify", *arguments, "--argo-applications", str(clone)]) == 0
        assert "CLEAN 2 fields readable with the new key and unchanged in content" in capsys.readouterr().out

        assert await tool.main(["--ja", "--verify", *arguments]) == 1

    assert f"FAIL field disappeared: {there}#<sops-document>" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_old_key_does_not_go_away_while_the_argocd_secrets_are_unwalked(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Same refusal ``--projects`` already had, for the same reason: an unwalked place.

    ``--remove-old-key`` is the point of no return -- afterwards nothing can decrypt what was
    left behind. A clean verdict over four of the five places is not a reason to throw the key
    away.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    old_file = tmp_path / "old_key.txt"
    old_file.write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    projects = tmp_path / "projects"
    projects.mkdir()
    await _project_file(projects, "een", new_public)

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
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

    printed = capsys.readouterr()
    assert code == 1
    assert "REFUSED the old key does not go away without --argo-applications" in printed.err
    assert "without --argo-applications the final check does not walk the ArgoCD" in printed.out
    assert old_file.exists()


@pytest.mark.asyncio
async def test_a_coverage_gap_stops_the_round_before_a_byte_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Converting over a gap is worse than not converting: it ends in a CLEAN that is not true.

    The round would succeed, the fingerprint would add up over the fields the tool knows, and
    ``--assert-old-key-dead`` would report the old key dead while it still opened the file
    nobody walked. So the gap stops the run, and the file it stops on keeps its old value.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    env_path = await _env_file(tmp_path / ".env", {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2"}, old_public)
    before = env_path.read_text()
    outsider = tmp_path / "forgotten.py"

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
        patch.object(tool, "files_with_ciphertext", return_value={outsider: 1}),
    ):
        code = await tool.main(
            [
                "--ja",
                *_key_files(tmp_path, old_private, new_private),
                "--fingerprint",
                str(tmp_path / "fingerprint.json"),
            ]
        )

    printed = capsys.readouterr()
    assert code == 1
    assert "carries ciphertext and nothing converts it" in printed.out
    assert "STOPPED a tracked file carries ciphertext that nothing here converts." in printed.err
    assert "LOOSE_VALUE_FILES" in printed.err
    assert env_path.read_text() == before
    assert not (tmp_path / "fingerprint.json").exists()


@pytest.mark.asyncio
async def test_the_final_check_refuses_to_call_the_old_key_dead_over_a_gap(tmp_path: Path) -> None:
    """The half that matters most: the verdict has to be about the tree, not about the worklist."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    outsider = tmp_path / "forgotten.py"

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
        patch.object(tool, "files_with_ciphertext", return_value={outsider: 1}),
    ):
        check = await tool.run_final_check(old_private, new_private, old_public, new_public, None, None)

    assert not check.clean
    assert check.outside_coverage == [str(outsider)]
    assert any("carries ciphertext and nothing converts it" in line for line in check.lines())
    assert "CLEAN the old key opens nothing, the new key opens everything" not in check.lines()


def test_a_new_file_with_real_ciphertext_is_a_gap_until_it_is_covered(tmp_path: Path) -> None:
    """The class, not the three instances: the next such file has to stop the tool by itself."""
    newcomer = tmp_path / "new-thing.py"

    with patch.object(tool, "files_with_ciphertext", return_value={newcomer: 1}):
        assert tool.coverage_gaps() == [newcomer]

        with patch.object(tool, "loose_paths", return_value=[newcomer]):
            assert tool.coverage_gaps() == []

        relative = "somewhere/new-thing.py"
        with patch.object(tool, "COVERAGE_EXCEPTIONS", {relative: "on a test key"}):
            assert tool.coverage_gaps() == [newcomer]
            with patch.object(tool, "files_with_ciphertext", return_value={tool.REPO / relative: 1}):
                assert tool.coverage_gaps() == []


def test_every_exception_names_a_file_that_is_really_there_and_really_holds_ciphertext() -> None:
    """An exception for a file that moved is an excuse with nothing behind it.

    The entry would keep standing, nobody would notice, and the file it once described could come
    back under another name outside the coverage -- which is the whole failure this guard exists
    for.
    """
    for name, reason in tool.COVERAGE_EXCEPTIONS.items():
        path = tool.REPO / name
        assert path.is_file(), f"{name} is on the exception list but not in the tree"
        assert reason.strip(), f"{name} stands on the exception list without a reason"
        assert path in files_with_ciphertext(tool.REPO), f"{name} no longer carries ciphertext"


@pytest.mark.asyncio
async def test_the_final_check_measures_the_exception_list_against_the_old_key(tmp_path: Path) -> None:
    """Every exception is a hand-written CLAIM, and hand-written coverage is what went wrong.

    So the one key that can settle it does: an excused file that turns out to open with the old
    key is named, and it does not disappear into the field count -- it was never converted, so
    counting it would put the fingerprint comparison off by exactly the number of excuses.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    # Indented, like the block scalar an e2e fixture really holds: the armor has to be
    # de-indented before it can be opened at all.
    block = await encrypt_age_content("hunter2", old_public)
    excused = tmp_path / "fixture.yaml"
    excused.write_text("password: |\n" + "".join(f"  {line}\n" for line in block.splitlines()))

    check = FinalCheck()
    with (
        patch.object(tool, "REPO", tmp_path),
        patch.object(tool, "COVERAGE_EXCEPTIONS", {"fixture.yaml": "an e2e fixture on a test key"}),
    ):
        await tool.check_exceptions(old_private, check)
        clean = FinalCheck()
        await tool.check_exceptions(new_private, clean)

    assert check.counted == 0
    assert check.still_opens_with_old == ["fixture.yaml (on the exception list: an e2e fixture on a test key)"]
    assert clean.still_opens_with_old == []


def test_ci_installs_sops_so_the_rotation_guards_actually_run() -> None:
    """A skip reads as green, and the SOPS half of this file is exactly what must not go quiet.

    Measured: the test job installed ``age`` but not ``sops``, so every test here that rotates a
    real SOPS file -- and the whole of ``test_sops_skip_unchanged`` -- skipped on the runner
    while the summary said passed.
    """
    workflow = yaml.safe_load((tool.REPO / ".github" / "workflows" / "ci.yml").read_text())
    # Steps that really run: a step behind a falsy condition installs nothing, and reading only
    # the "run" lines would call that wired up.
    installs = [
        step.get("run", "")
        for step in workflow["jobs"]["test"]["steps"]
        if str(step.get("if", "true")).strip().lower() not in {"false", "${{ false }}"}
    ]

    assert any("sops" in command and "chmod +x" in command for command in installs)
    dockerfile = (tool.REPO / "operations-manager" / "Dockerfile").read_text()
    pinned = re.search(r"ARG SOPS_VERSION=(v[\d.]+)", dockerfile)
    assert pinned is not None
    assert any(pinned.group(1) in command for command in installs), (
        f"CI must install the same sops as the image ({pinned.group(1)})"
    )


# ---------------------------------------------------------------------------
# the two actions that work on a FILE: through main(), on an answered path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_removing_the_old_key_follows_the_answered_path_not_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An operator who answers the prompt with another path must see THAT file go away.

    The tests above pass the paths as flags, so the default and the answer always coincide and
    the difference between them cannot show. Here the prompt is answered with a name that is not
    the default: before the fix the run printed "Old key removed: .../security/old_key.txt" and
    exited 0 while the real key file was still on disk -- a false report on the last safety
    action of the plan.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    answered_old = tmp_path / "mijn-oude-sleutel.txt"
    answered_old.write_text(f"{old_private}\n")
    answered_new = tmp_path / "mijn-nieuwe-sleutel.txt"
    answered_new.write_text(f"{new_private}\n")
    projects = tmp_path / "projects"
    projects.mkdir()
    await _project_file(projects, "een", new_public)
    argo = tmp_path / "zad-argo-user-applications"
    argo.mkdir()

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[]),
        patch("builtins.input", side_effect=[str(answered_old), str(answered_new)]),
    ):
        code = await tool.main(
            [
                "--remove-old-key",
                "--projects",
                str(projects),
                "--argo-applications",
                str(argo),
                "--fingerprint",
                str(tmp_path / "absent.json"),
            ]
        )

    output = capsys.readouterr().out
    assert code == 0
    assert not answered_old.exists()
    assert f"Old key removed: {answered_old}" in output


@pytest.mark.asyncio
async def test_renaming_follows_the_answered_path_not_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--rename`` on answered paths has to move those files, not compare the defaults.

    The defaults ARE ``CANONICAL_OLD`` and ``CANONICAL_NEW`` by construction, so reading the
    paths from argparse made the first check in ``rename_keys`` always true: it printed "Keys
    already sit under their fixed names." and renamed nothing, whatever the operator answered.
    """
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    answered_old = tmp_path / "mijn-oude-sleutel.txt"
    answered_old.write_text(f"{old_private}\n")
    answered_new = tmp_path / "mijn-nieuwe-sleutel.txt"
    answered_new.write_text(f"{new_private}\n")
    target_old = tmp_path / "security" / "old_key.txt"
    target_new = tmp_path / "security" / "key.txt"
    target_old.parent.mkdir()

    with (
        patch.object(tool, "CANONICAL_OLD", target_old),
        patch.object(tool, "CANONICAL_NEW", target_new),
        patch("builtins.input", side_effect=[str(answered_old), str(answered_new), "ja"]),
    ):
        code = await tool.main(["--rename"])

    output = capsys.readouterr().out
    assert code == 0
    assert target_old.read_text() == f"{old_private}\n"
    assert target_new.read_text() == f"{new_private}\n"
    assert not answered_old.exists()
    assert not answered_new.exists()
    assert "Renamed." in output


@pytest.mark.asyncio
async def test_the_note_about_a_missing_old_key_names_the_answered_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The third action on an answered path, and the one that only PRINTS a path.

    ``--verify`` is allowed to run without the old key, and says so in a NOTE. That note named
    ``arguments.old_key`` instead of the answered path, so an operator who answers the prompt
    with another name was pointed at the default -- here a file that exists and holds a
    perfectly good key. Same class as the two above, and invisible to every other test because
    they all pass ``--old-key`` and then answer and default coincide.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    env_path = await _env_file(
        tmp_path / ".env",
        {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2", "GIT_ARGO_APPLICATIONS_PASSWORD": "x"},
        old_public,
    )
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(tmp_path / "fingerprint.json")]
    answered_old = tmp_path / "mijn-oude-sleutel.txt"

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
    ):
        assert await tool.main(["--ja", *arguments]) == 0
        capsys.readouterr()
        with patch("builtins.input", side_effect=[str(answered_old), str(tmp_path / "key.txt")]):
            code = await tool.main(["--verify", *arguments])

    printed = capsys.readouterr().out
    assert code == 0
    assert f"NOTE file does not exist: {answered_old}" in printed
    assert str(tmp_path / "old_key.txt") not in printed
    assert "CLEAN 2 fields readable with the new key and unchanged in content" in printed


@pytest.mark.asyncio
async def test_an_old_key_file_without_a_key_line_is_not_reported_as_an_absent_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Two different failures, and the NOTE flattened them into the wrong one.

    A file that IS there but carries no key line is a different problem from a file that is
    gone: the first is a file to look at, the second is the expected state after step 8. Reported
    as "no old key at <path>" the operator reads it as gone and stops looking at the file that is
    lying there.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    env_path = await _env_file(
        tmp_path / ".env",
        {"GIT_PROJECTS_SERVER_PASSWORD": "hunter2", "GIT_ARGO_APPLICATIONS_PASSWORD": "x"},
        old_public,
    )
    arguments = [*_key_files(tmp_path, old_private, new_private), "--fingerprint", str(tmp_path / "fingerprint.json")]

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "loose_paths", return_value=[env_path]),
    ):
        assert await tool.main(["--ja", *arguments]) == 0
        (tmp_path / "old_key.txt").write_text("# a file of nothing but comments\n# no key line here\n")
        capsys.readouterr()
        code = await tool.main(["--ja", "--verify", *arguments])

    printed = capsys.readouterr().out
    assert code == 0
    assert f"NOTE no {AGE_KEY_MARKER} line in {tmp_path / 'old_key.txt'}" in printed
    assert "no old key at" not in printed
    assert "CLEAN 2 fields readable with the new key and unchanged in content" in printed


def test_the_operator_script_runs_through_the_four_phases_and_each_command_sits_in_its_own() -> None:
    """The phase a command sits in is what the doc promises.

    The boundary that matters is between VERIFY-1 and APPLY: a command that drifts across it
    turns "nothing has left this machine" into a lie. An operator runs this doc by hand, so
    nothing else would catch that.
    """
    text = (tool.REPO / "features" / "sops-sleutel-vervangen.md").read_text()
    headings = ["### PREPARE", "### VERIFY-1", "### APPLY", "### VERIFY-2", "### Daarna"]
    starts = [text.index(heading) for heading in headings]

    assert starts == sorted(starts), "the phases run out of order"

    phase = dict(zip(headings, [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)], strict=True))

    assert f"{LAUNCHER} scripts/rotate-project-keys.py --projects" in phase["### PREPARE"]
    # Every rotate-sops-key call in PREPARE except the key-making one converts, and the fifth
    # place is converted here or nowhere -- VERIFY-1 would find it, one phase and a clone late.
    converting = [
        line.strip()
        for line in phase["### PREPARE"].splitlines()
        if line.strip().startswith(f"{LAUNCHER} scripts/rotate-sops-key.py") and "--rename" not in line
    ]
    assert converting, "PREPARE stopped running the round at all"
    for line in converting:
        assert "--argo-applications" in line, f"this leaves the ArgoCD secrets on the old key: {line}"
    assert f"{LAUNCHER} scripts/set-sops-key-secret.py" not in phase["### PREPARE"], "the swap is not a preparation"
    assert "--assert-old-key-dead" in phase["### VERIFY-1"], "the go/no-go check is the point of VERIFY-1"
    assert "kustomize build" in phase["### VERIFY-1"], "a full render is checked before ArgoCD gets to try it"
    assert f"{LAUNCHER} scripts/set-sops-key-secret.py" in phase["### APPLY"]
    assert "--assert-old-key-dead" in phase["### VERIFY-2"]
    assert "--remove-old-key" in phase["### Daarna"], "throwing the old key away is not part of the cutover"

    # Rename a heading and the "still to do" list the round prints goes stale without saying so.
    closing = Path(tool.__file__).read_text()
    for heading in headings[:-1]:
        assert heading.removeprefix("### ") in closing, f"the script stopped naming {heading}"
