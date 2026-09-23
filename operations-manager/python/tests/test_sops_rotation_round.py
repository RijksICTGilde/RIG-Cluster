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
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml
from opi.utils.age import BASE64_AGE_PREFIX, encrypt_age_content
from opi.utils.sops import encrypt_to_sops_files, generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_path

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import sops_rotation as tool  # noqa: E402
from key_rotation import (  # noqa: E402
    ConversionFailed,
    Fingerprint,
    env_fields,
    opens_with,
    sha256_of,
    sops_files_for,
    sops_plaintext,
    sops_recipients,
    sops_rotate,
)

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


# ---------------------------------------------------------------------------
# the SOPS files themselves: the first of the four places, and the one the task is named after
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

    with _selecting_from(tmp_path), patch.object(tool, "env_paths", return_value=[]):
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

    with _selecting_from(tmp_path), patch.object(tool, "env_paths", return_value=[]):
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

    with _selecting_from(sops_path.parent), patch.object(tool, "env_paths", return_value=[env_path]):
        code = await tool.main(
            ["--ja", "--dry-run", *_key_files(tmp_path, old_private, new_private), "--fingerprint", str(fingerprint)]
        )

    assert code == 0
    printed = capsys.readouterr().out
    assert "1 SOPS files on the old recipient" in printed
    assert "1 loose base64+age: values on the old key" in printed
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

    with _selecting_from(sops_path.parent), patch.object(tool, "env_paths", return_value=[env_path]):
        code = await tool.main(["--ja", *arguments])
        capsys.readouterr()
        again = await tool.main(["--ja", *arguments])

    assert code == 0
    assert again == 0
    assert "Nothing to do: no field sits on the old key any more." in capsys.readouterr().out
    assert sops_recipients(sops_path) == [new_public]
    assert sops_plaintext(sops_path, old_private) is None
    assert not await opens_with(tool.all_env_fields([env_path])[0].value, old_private)

    written = fingerprint.read_text()
    assert len(Fingerprint.load(fingerprint).fields) == 2
    assert "hunter2" not in written
    assert "AGE-SECRET-KEY-" not in written


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

    with _selecting_from(sops_path.parent), patch.object(tool, "env_paths", return_value=[env_path]):
        assert await tool.main(["--ja", *arguments]) == 0
        capsys.readouterr()

        assert await tool.main(["--ja", "--verify", *arguments]) == 0
        assert "CLEAN 2 fields readable with the new key and unchanged in content" in capsys.readouterr().out

        # Put the SOPS file back on the old key: the same content, but the new key cannot read it.
        sops_rotate(sops_path, new_public, old_public, new_private)
        assert await tool.main(["--ja", "--verify", *arguments]) == 1

    assert f"FAIL does not open with the new key: {sops_path}#<sops-document>" in capsys.readouterr().out


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

    with _selecting_from(sops_path.parent), patch.object(tool, "env_paths", return_value=[env_path]):
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


def test_every_configured_env_file_is_really_there_and_holds_an_encrypted_value() -> None:
    """The row that gets forgotten: ``sops rotate`` does not see a loose ``base64+age:`` value.

    ``env_paths()`` drops a path that does not exist, so a moved or renamed configmap costs the
    tool a file WITHOUT any complaint -- the round would pass, the final check would pass, and
    that value would stay on the old key. This is the assertion that turns such a move into a
    red test instead of a silent miss.
    """
    missing = [name for name in tool.ENV_FILES if not (tool.REPO / name).is_file()]

    assert missing == []
    assert "operations-manager/python/.env" in tool.ENV_FILES
    for path in tool.env_paths():
        assert env_fields(path), f"{path} carries no base64+age: value any more"


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
    new_private, _new_public = generate_sops_key_pair()
    answered_old = tmp_path / "mijn-oude-sleutel.txt"
    answered_old.write_text(f"{old_private}\n")
    answered_new = tmp_path / "mijn-nieuwe-sleutel.txt"
    answered_new.write_text(f"{new_private}\n")
    projects = tmp_path / "projects"
    projects.mkdir()

    with (
        patch.object(tool, "sops_files_for", return_value=[]),
        patch.object(tool, "env_paths", return_value=[]),
        patch("builtins.input", side_effect=[str(answered_old), str(answered_new)]),
    ):
        code = await tool.main(
            [
                "--remove-old-key",
                "--projects",
                str(projects),
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
