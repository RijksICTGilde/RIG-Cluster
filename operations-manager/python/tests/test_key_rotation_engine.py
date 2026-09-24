"""Tests for scripts/key_rotation.py, the engine under the AGE key rotation.

The tool's promise is not "it ran without an error" but "nothing changed except the key".
That is what these tests measure, on real ciphertext produced by the real ``age`` binary:

* the one loop keeps the plaintext and the storage form, and changes the ciphertext;
* the same loop with a replacement value changes the plaintext at exactly that field;
* a value converted for B no longer opens with A;
* the fingerprint catches a lost field, a shifted value and a replacement that did not land;
* the recipient selection leaves a file on another key alone;
* a project file keeps every other field, including its comments and its multi-line
  ``age-private-key`` block scalar.

Both keys are generated per test through ``generate_sops_key_pair`` -- the same helper OPI
uses when it makes a project key. No fixed key appears in this file: a key in a test file is
what put this task on the list in the first place.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from opi.utils.age import BASE64_AGE_PREFIX, decrypt_age_content, encrypt_age_content
from opi.utils.sops import generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_path, load_yaml_from_string

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from key_rotation import (  # noqa: E402
    PROJECT_FIELD_PRIVATE_KEY,
    ConversionFailed,
    FinalCheck,
    Fingerprint,
    Form,
    MissingKey,
    check_value,
    convert_value,
    decrypt_field,
    encrypted_candidates,
    files_with_ciphertext,
    form_of,
    is_real_ciphertext,
    loose_values,
    opens_with,
    plaintext_secrets,
    project_fields,
    project_plain_passwords,
    public_key_of,
    read_key,
    rotate_project_file,
    set_project_field,
    sha256_of,
    sops_files,
    sops_files_for,
    sops_recipients,
    write_loose_value,
)

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="requires the age binary")


@pytest.fixture
def key_pair_a() -> tuple[str, str]:
    """A throwaway keypair standing in for the old platform key."""
    return generate_sops_key_pair()


@pytest.fixture
def key_pair_b() -> tuple[str, str]:
    """A second throwaway keypair standing in for the new platform key."""
    return generate_sops_key_pair()


async def _base64_value(plaintext: str, public_key: str) -> str:
    block = await encrypt_age_content(plaintext, public_key)
    return f"{BASE64_AGE_PREFIX}{base64.b64encode(block.encode()).decode()}"


# ---------------------------------------------------------------------------
# reading the key
# ---------------------------------------------------------------------------


def test_read_key_finds_the_marker_line_not_the_third_line(tmp_path: Path, key_pair_a: tuple[str, str]) -> None:
    """An extra comment line must not shift the key out of reach."""
    private, _public = key_pair_a
    path = tmp_path / "key.txt"
    path.write_text(f"# created: today\n# note: an extra line\n# public key: xxx\n{private}\n")
    assert read_key(path) == private


def test_read_key_names_the_path_when_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(MissingKey) as caught:
        read_key(tmp_path / "nope.txt")
    assert str(tmp_path / "nope.txt") in str(caught.value)


def test_read_key_names_the_path_when_there_is_no_key_in_it(tmp_path: Path) -> None:
    path = tmp_path / "key.txt"
    path.write_text("# only comments\n")
    with pytest.raises(MissingKey) as caught:
        read_key(path)
    assert str(path) in str(caught.value)


def test_public_key_is_derived_and_not_asked_for(key_pair_a: tuple[str, str]) -> None:
    private, public = key_pair_a
    assert public_key_of(private) == public


# ---------------------------------------------------------------------------
# the one loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recrypt_keeps_the_plaintext_and_changes_the_ciphertext(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    private_a, public_a = key_pair_a
    _private_b, public_b = key_pair_b
    before = await encrypt_age_content("hunter2", public_a)

    conversion = await convert_value(before, private_a, public_b)

    assert conversion.content_unchanged
    assert conversion.sha256_before == sha256_of("hunter2")
    assert conversion.new_value != before
    assert not conversion.replaced


@pytest.mark.asyncio
async def test_recrypt_moves_the_value_to_the_new_key_only(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """The whole point of no dual recipients: A stops opening it.

    Without this assertion a rotation that adds B while leaving A on the file would pass
    every other test in this file.
    """
    private_a, public_a = key_pair_a
    private_b, public_b = key_pair_b
    before = await encrypt_age_content("hunter2", public_a)

    conversion = await convert_value(before, private_a, public_b)

    assert await decrypt_field(conversion.new_value, private_b) == "hunter2"
    assert not await opens_with(conversion.new_value, private_a)


@pytest.mark.asyncio
async def test_the_same_loop_with_a_new_value_replaces_the_plaintext(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """One loop, two entry points: the PAT replacement differs only in this argument."""
    private_a, public_a = key_pair_a
    private_b, public_b = key_pair_b
    before = await encrypt_age_content("ghp_old", public_a)

    conversion = await convert_value(before, private_a, public_b, new_plaintext="ghp_new")

    assert conversion.replaced
    assert not conversion.content_unchanged
    assert conversion.sha256_after == sha256_of("ghp_new")
    assert await decrypt_field(conversion.new_value, private_b) == "ghp_new"


@pytest.mark.asyncio
async def test_the_storage_form_survives_the_conversion(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """A base64+age: value must not come back as a multi-line block, or the env line breaks."""
    private_a, public_a = key_pair_a
    _private_b, public_b = key_pair_b
    block = await encrypt_age_content("hunter2", public_a)
    prefixed = await _base64_value("hunter2", public_a)

    from_block = await convert_value(block, private_a, public_b)
    from_prefixed = await convert_value(prefixed, private_a, public_b)

    assert form_of(from_block.new_value) is Form.BLOCK
    assert form_of(from_prefixed.new_value) is Form.BASE64
    assert "\n" not in from_prefixed.new_value


@pytest.mark.asyncio
async def test_a_value_that_does_not_open_with_the_old_key_is_refused(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    private_a, _public_a = key_pair_a
    _private_b, public_b = key_pair_b
    # age itself raises; the assertion is that the loop does not quietly re-encrypt garbage.
    with pytest.raises(Exception, match=r"[Aa]ge decryption failed"):
        await convert_value(await encrypt_age_content("x", public_b), private_a, public_b)


@pytest.mark.asyncio
async def test_plain_text_never_reaches_the_loop(key_pair_a: tuple[str, str]) -> None:
    private_a, _public_a = key_pair_a
    with pytest.raises(ConversionFailed):
        await convert_value("just-a-password", private_a, "age1whatever")


# ---------------------------------------------------------------------------
# the fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_holds_no_secret_only_a_hash(tmp_path: Path) -> None:
    afterwards = tmp_path / "fingerprint.json"
    fingerprint = Fingerprint()
    fingerprint.set("some/file#FIELD", sha256_of("hunter2"))
    fingerprint.save(afterwards)

    written = afterwards.read_text()
    assert "hunter2" not in written
    assert sha256_of("hunter2") in written
    assert Fingerprint.load(afterwards).fields == fingerprint.fields


def test_fingerprint_reports_a_field_that_disappeared() -> None:
    before = Fingerprint(fields={"a": "1", "b": "2"})
    after = Fingerprint(fields={"a": "1"})
    assert before.compare(after) == ["field disappeared: b"]


def test_fingerprint_reports_a_field_that_appeared() -> None:
    before = Fingerprint(fields={"a": "1"})
    after = Fingerprint(fields={"a": "1", "b": "2"})
    assert before.compare(after) == ["field appeared: b"]


def test_fingerprint_reports_a_value_that_shifted() -> None:
    before = Fingerprint(fields={"a": "1"})
    after = Fingerprint(fields={"a": "changed"})
    assert before.compare(after) == ["content changed: a"]


def test_fingerprint_accepts_a_difference_only_where_a_replacement_was_announced() -> None:
    before = Fingerprint(fields={"pat": "1", "key": "2"})
    after = Fingerprint(fields={"pat": "changed", "key": "2"})
    assert before.compare(after, replaced=["pat"]) == []


def test_fingerprint_reports_a_replacement_that_did_not_land() -> None:
    """The other half of the PAT case: an unchanged hash there is a failure too."""
    before = Fingerprint(fields={"pat": "1"})
    after = Fingerprint(fields={"pat": "1"})
    assert before.compare(after, replaced=["pat"]) == ["replacement did not land, content unchanged: pat"]


# ---------------------------------------------------------------------------
# the final check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_check_is_clean_when_the_old_key_opens_nothing(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    private_a, _public_a = key_pair_a
    private_b, public_b = key_pair_b
    check = FinalCheck()
    await check_value("f", await encrypt_age_content("x", public_b), private_a, private_b, check)
    assert check.clean
    assert "CLEAN the old key opens nothing, the new key opens everything" in check.lines()


@pytest.mark.asyncio
async def test_final_check_names_a_field_that_was_skipped(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """A deliberately skipped field must make the check fail, by name."""
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    check = FinalCheck()
    await check_value("skipped/file#FIELD", await encrypt_age_content("x", public_a), private_a, private_b, check)
    assert not check.clean
    assert check.still_opens_with_old == ["skipped/file#FIELD"]
    assert any("STILL opens with the old key: skipped/file#FIELD" in line for line in check.lines())


@pytest.mark.asyncio
async def test_final_check_fails_when_the_count_does_not_match(
    key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """Every field converted and readable, but one fewer than before: still not clean."""
    private_a, _public_a = key_pair_a
    private_b, public_b = key_pair_b
    check = FinalCheck(expected=2)
    await check_value("f", await encrypt_age_content("x", public_b), private_a, private_b, check)
    assert not check.count_matches
    assert not check.clean


# ---------------------------------------------------------------------------
# selection by recipient
# ---------------------------------------------------------------------------


def _sops_stub(recipient: str) -> str:
    """The shape the selection reads: a sops block with one age recipient."""
    return (
        "apiVersion: v1\nkind: Secret\nstringData:\n"
        "    password: ENC[AES256_GCM,data:abc,iv:def,tag:ghi,type:str]\n"
        "sops:\n    age:\n        - recipient: " + recipient + "\n          enc: |\n            xxx\n"
    )


def test_sops_files_finds_a_file_without_the_sops_suffix(tmp_path: Path) -> None:
    """Two real files carry the metadata without ``.sops.yaml`` in the name."""
    (tmp_path / "operations-manager-env-secrets.yaml").write_text(_sops_stub("age1aaa"))
    assert [path.name for path in sops_files(tmp_path)] == ["operations-manager-env-secrets.yaml"]


def test_selection_by_recipient_leaves_another_key_alone(tmp_path: Path) -> None:
    """A file on another key must not be touched: the sandbox key is a different key."""
    (tmp_path / "platform.sops.yaml").write_text(_sops_stub("age1platform"))
    (tmp_path / "practice.sops.yaml").write_text(_sops_stub("age1practice"))

    selected = sops_files_for(tmp_path, "age1platform")

    assert [path.name for path in selected] == ["platform.sops.yaml"]
    assert sops_recipients(tmp_path / "practice.sops.yaml") == ["age1practice"]


def test_selection_reads_the_flat_recipient_form_too(tmp_path: Path) -> None:
    """Three real files indent ``recipient:`` without a list dash; those must not fall out."""
    (tmp_path / "flat.sops.yaml").write_text(
        "stringData:\n    a: ENC[x]\nsops:\n    age:\n        enc: |\n            x\n        recipient: age1flat\n"
    )
    assert [path.name for path in sops_files_for(tmp_path, "age1flat")] == ["flat.sops.yaml"]


# ---------------------------------------------------------------------------
# loose values
# ---------------------------------------------------------------------------

#: A stand-in with the right SHAPE and no content, long enough to pass the length floor the
#: pattern puts on a value so prose about ``base64+age:`` is not a hit.
SHAPED = "base64+age:QUJDREVGR0hJSktMTU5PUFFSUw=="
SHAPED_TOO = "base64+age:WFlaMDEyMzQ1Njc4OWFiY2RlZg=="


def test_loose_values_finds_the_value_inside_a_yaml_literal_block(tmp_path: Path) -> None:
    """The configmap keeps its env content in a block scalar; the scan is line based."""
    path = tmp_path / "configmap.yaml"
    path.write_text(
        "data:\n  .env: |\n    CLUSTER_MANAGER=odcn-production\n"
        f"    GIT_PROJECTS_SERVER_PASSWORD={SHAPED}\n"
        "    GIT_PROJECTS_SERVER_BRANCH=main\n"
    )
    found = loose_values(path)
    assert [(f.key, f.line_number) for f in found] == [("GIT_PROJECTS_SERVER_PASSWORD", 4)]


def test_loose_values_finds_the_value_in_a_python_literal(tmp_path: Path) -> None:
    """The shape that was outside the tool until a review measured the tree with the old key.

    ``opi/core/config.py`` holds the platform-keyed default of ``PROJECT_REPO_PASSWORD`` as a
    quoted Python literal, and the migration script next to it holds the same value as a dict
    entry. An env-shaped pattern (``^KEY=value$``) matched neither.
    """
    path = tmp_path / "config.py"
    path.write_text(f'    PROJECT_REPO_PASSWORD: str = "{SHAPED}"\n    OTHER = 1\n')
    assert [(f.key, f.line_number) for f in loose_values(path)] == [("PROJECT_REPO_PASSWORD", 1)]


def test_loose_values_names_a_dict_entry_after_its_key(tmp_path: Path) -> None:
    path = tmp_path / "migrate.py"
    path.write_text(f'        "password": "{SHAPED}",\n')
    assert [f.key for f in loose_values(path)] == ["password"]


def test_loose_values_keeps_two_values_on_one_name_apart(tmp_path: Path) -> None:
    """The fingerprint is a dict on ``path#name``; a repeated name would drop one silently."""
    path = tmp_path / "two.py"
    path.write_text(f'"password": "{SHAPED}",\n"password": "{SHAPED_TOO}",\n')
    assert [f.key for f in loose_values(path)] == ["password", "password:2"]


def test_loose_values_reads_a_file_that_is_one_armored_block(tmp_path: Path) -> None:
    """``projects/age-secret-github.txt`` is the whole value and has no key line at all."""
    path = tmp_path / "age-secret-github.txt"
    path.write_text("-----BEGIN AGE ENCRYPTED FILE-----\nQUJDREVG\n-----END AGE ENCRYPTED FILE-----\n")
    found = loose_values(path)
    assert [(f.key, f.line_number) for f in found] == [("<file>", None)]


def test_loose_values_ignores_a_plain_value(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("GIT_PROJECTS_SERVER_PASSWORD=plain:hunter2\nOTHER=1\n")
    assert loose_values(path) == []


def test_loose_values_ignores_prose_about_the_prefix(tmp_path: Path) -> None:
    """A doc that names the form is not a value; the length floor is what tells them apart."""
    path = tmp_path / "README.md"
    path.write_text("The value is written as base64+age:<base64> in an env line.\n")
    assert loose_values(path) == []


def test_write_loose_value_keeps_the_indentation_and_the_other_lines(tmp_path: Path) -> None:
    path = tmp_path / "configmap.yaml"
    path.write_text(f"data:\n  .env: |\n    A={SHAPED}\n    B=keep-me\n")

    field_ = loose_values(path)[0]
    write_loose_value(field_, SHAPED_TOO)

    assert path.read_text() == f"data:\n  .env: |\n    A={SHAPED_TOO}\n    B=keep-me\n"


def test_write_loose_value_rewrites_a_whole_file_block(tmp_path: Path) -> None:
    path = tmp_path / "age-secret-github.txt"
    path.write_text("-----BEGIN AGE ENCRYPTED FILE-----\nQUJDREVG\n-----END AGE ENCRYPTED FILE-----\n")

    write_loose_value(
        loose_values(path)[0], "-----BEGIN AGE ENCRYPTED FILE-----\nWFla\n-----END AGE ENCRYPTED FILE-----"
    )

    assert path.read_text().strip().splitlines()[1] == "WFla"


def test_write_loose_value_refuses_when_the_line_moved(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(f"A={SHAPED}\n")
    field_ = loose_values(path)[0]
    path.write_text("A=something-else\n")
    with pytest.raises(ConversionFailed):
        write_loose_value(field_, SHAPED_TOO)


def test_write_loose_value_leaves_no_temporary_behind_when_it_is_interrupted(tmp_path: Path) -> None:
    """The half of the writer that is not about the target file: what the failure leaves next to it.

    The temporary is a dotfile in the directory of the file being converted, so in this repo it
    sits in ``bootstrap/`` or next to ``.env`` and a following ``git add -A`` picks it up. Ctrl-C
    during the move is a ``KeyboardInterrupt``: not an ``OSError``, so a named list of failure
    types would step over the cleanup and leave it there. ``argo_rotation.write_repository_secret``
    names this function as the reason it catches just as widely, which only holds while it does.
    """
    path = tmp_path / "configmap.yaml"
    before = f"data:\n  .env: |\n    A={SHAPED}\n    B=keep-me\n"
    path.write_text(before)
    field_ = loose_values(path)[0]

    with (
        patch("key_rotation.os.replace", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        write_loose_value(field_, SHAPED_TOO)

    assert list(tmp_path.iterdir()) == [path], "the temporary outlived the interrupt"
    assert path.read_text() == before


# ---------------------------------------------------------------------------
# the coverage guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_ciphertext_is_told_apart_from_a_value_with_only_the_shape(
    key_pair_a: tuple[str, str],
) -> None:
    """What keeps the exception list short enough to stay true: shape is not content."""
    private_key, public_key = key_pair_a
    assert await decrypt_age_content(await encrypt_age_content("hunter2", public_key), private_key) == "hunter2"

    assert is_real_ciphertext(await encrypt_age_content("hunter2", public_key))
    assert is_real_ciphertext(
        BASE64_AGE_PREFIX + base64.b64encode((await encrypt_age_content("hunter2", public_key)).encode()).decode()
    )

    assert not is_real_ciphertext(SHAPED)
    assert not is_real_ciphertext("-----BEGIN AGE ENCRYPTED FILE-----\nQUJDREVG\n-----END AGE ENCRYPTED FILE-----")
    assert not is_real_ciphertext("plain:hunter2")

    # A SHORTENED block, and this one is not made up: two values in this tree open with the
    # header and a recipient stanza and stop before the MAC line. Reading only the header would
    # call those real, which is how the exception list starts filling up with entries nobody
    # can act on.
    body = base64.b64decode("".join((await encrypt_age_content("hunter2", public_key)).splitlines()[1:-1]))
    cut = base64.b64encode(body[: body.index(b"\n--- ")]).decode()
    assert not is_real_ciphertext(f"-----BEGIN AGE ENCRYPTED FILE-----\n{cut}\n-----END AGE ENCRYPTED FILE-----")


@pytest.mark.asyncio
async def test_a_block_is_read_even_when_it_sits_indented_in_a_yaml_file(key_pair_a: tuple[str, str]) -> None:
    """An e2e fixture keeps its block in a block scalar, and the guard has to be able to open it.

    ``age`` refuses an indented armor, so without the de-indent every excused file would answer
    "does not open with the old key" for the wrong reason -- and the check on the exception list
    would be a check on nothing.
    """
    private_key, public_key = key_pair_a
    block = await encrypt_age_content("hunter2", public_key)
    indented = "password: |\n" + "".join(f"  {line}\n" for line in block.splitlines())

    found = [candidate for candidate in encrypted_candidates(indented) if is_real_ciphertext(candidate)]

    assert len(found) == 1
    assert await decrypt_field(found[0], private_key) == "hunter2"


@pytest.mark.asyncio
async def test_the_inventory_walks_tracked_files_and_not_the_working_tree(
    tmp_path: Path, key_pair_a: tuple[str, str]
) -> None:
    """``security/`` holds the real keys on purpose and is untracked; a scratch file is not a gap.

    Measured on a real git tree holding the SAME ciphertext twice, once added and once not.
    The inventory is what the coverage guard refuses to start on, so reading the working tree
    would turn every scratch file and every untracked clone into a blockade instead of a guard.

    Tracked is the only question it asks. Where a file SITS is not one: a committed bundle under
    ``dist/`` carrying ciphertext is exactly the thing the coverage guard has to see, and the
    filter this shares with the scanner used to drop those on a directory rule.
    """
    _private_key, public_key = key_pair_a
    block = await encrypt_age_content("hunter2", public_key)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "tracked.txt").write_text(block)
    (tmp_path / "scratch.txt").write_text(block)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text(block)
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt", "dist/bundle.js"], check=True)

    assert files_with_ciphertext(tmp_path) == {
        tmp_path / "tracked.txt": 1,
        tmp_path / "dist" / "bundle.js": 1,
    }


# ---------------------------------------------------------------------------
# project files
# ---------------------------------------------------------------------------


PROJECT_TEMPLATE = """\
schema-version: 2
name: proefproject
display-name: Proefproject
description: Project for the rotation test
# A comment a naive YAML round trip throws away
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
    user-env-vars: |-
{project_secret}
deployments:
  - name: deployment-1
    cluster: odcn-production
    namespace: proefproject
    repository: main-repo
    components:
      - reference: component-1
        image: ghcr.io/example/app:1
config:
  age-public-key: {project_public}
  age-private-key: |-
{project_private}
  api-key: {api_key}
"""


def _loaded(text: str) -> dict:
    """Load YAML from a string through the canonical loader, for before/after comparison."""
    loaded = load_yaml_from_string(text)
    assert loaded is not None
    return loaded


def _indent(value: str, spaces: int = 4) -> str:
    return "\n".join(" " * spaces + line for line in value.splitlines())


async def _write_project(path: Path, platform_public: str, project_pair: tuple[str, str]) -> tuple[str, str]:
    """Write a project file shaped like the real ones. Returns (repo password, api key) plaintext."""
    project_private, project_public = project_pair
    repo_password = await _base64_value("ghp_repository_token", platform_public)
    api_key = await _base64_value("the-api-key", project_public)
    path.write_text(
        PROJECT_TEMPLATE.format(
            repo_password=repo_password,
            project_secret=_indent(await encrypt_age_content("USER_VAR=1", project_public), 6),
            project_public=project_public,
            project_private=_indent(await encrypt_age_content(project_private, platform_public)),
            api_key=api_key,
        )
    )
    return "ghp_repository_token", "the-api-key"


@pytest.mark.asyncio
async def test_project_fields_finds_exactly_the_two_platform_fields(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """Both, not one: a project with only the first converted loses its repository access."""
    _private_a, public_a = key_pair_a
    path = tmp_path / "proefproject.yaml"
    await _write_project(path, public_a, key_pair_b)

    names = [name for name, _value in project_fields(load_yaml_from_path(str(path)))]
    assert names == [PROJECT_FIELD_PRIVATE_KEY, "repositories[0].password"]


def test_a_repositories_entry_that_is_not_a_mapping_yields_nothing_instead_of_crashing() -> None:
    """A hand-edited project file must not take down the last gate before the old key is deleted.

    ``repositories:`` holding a bare string is malformed but it is YAML that loads, and both
    walks reach into every entry of that list. Without the guard the final check dies on an
    AttributeError halfway through the tree: no verdict, no list of what it had already passed,
    and the run that was meant to say "the old key opens nothing" says nothing at all.

    Both selections in one test because they read the same list and carry the same guard: the
    key half by ciphertext, the token half by everything else.
    """
    data = {"name": "een", "repositories": ["main-repo", None, {"name": "real", "password": "plain:a-password"}]}

    assert project_fields(data) == []
    assert project_plain_passwords(data) == [("repositories[2].password", "a-password")]


@pytest.mark.asyncio
async def test_rotating_a_project_file_keeps_everything_else(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """The measurement that matters: only the two platform fields differ, content and all."""
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    project_pair = generate_sops_key_pair()
    path = tmp_path / "proefproject.yaml"
    repo_password, api_key = await _write_project(path, public_a, project_pair)
    before = path.read_text()

    report = await rotate_project_file(path, private_a, private_b, dry_run=False)

    assert report.skipped is None
    assert report.rewritten
    assert report.fields == [PROJECT_FIELD_PRIVATE_KEY, "repositories[0].password"]

    after_data = load_yaml_from_path(str(path))
    # The platform fields open with B and no longer with A.
    assert await decrypt_field(after_data["config"]["age-private-key"], private_b) == project_pair[0]
    assert await decrypt_field(after_data["repositories"][0]["password"], private_b) == repo_password
    assert not await opens_with(after_data["config"]["age-private-key"], private_a)
    assert not await opens_with(after_data["repositories"][0]["password"], private_a)
    # Everything the platform key does not own is byte-identical.
    assert after_data["config"]["api-key"] == _loaded(before)["config"]["api-key"]
    assert (
        await decrypt_age_content(
            base64.b64decode(after_data["config"]["api-key"][len(BASE64_AGE_PREFIX) :]).decode(), project_pair[0]
        )
        == api_key
    )
    # The comment survives, and the private key is still a block scalar and not one long line.
    assert "# A comment a naive YAML round trip throws away" in path.read_text()
    assert "age-private-key: |" in path.read_text()
    assert "\\n" not in path.read_text()


@pytest.mark.asyncio
async def test_rotating_a_project_file_twice_does_nothing_the_second_time(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    path = tmp_path / "proefproject.yaml"
    await _write_project(path, public_a, generate_sops_key_pair())

    await rotate_project_file(path, private_a, private_b, dry_run=False)
    after_first = path.read_text()
    second = await rotate_project_file(path, private_a, private_b, dry_run=False)

    assert second.skipped is not None
    assert "already converted" in second.skipped
    assert not second.rewritten
    assert path.read_text() == after_first


@pytest.mark.asyncio
async def test_a_dry_run_on_a_project_file_writes_nothing(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    path = tmp_path / "proefproject.yaml"
    await _write_project(path, public_a, generate_sops_key_pair())
    before = path.read_text()

    report = await rotate_project_file(path, private_a, private_b, dry_run=True)

    assert report.fields == [PROJECT_FIELD_PRIVATE_KEY, "repositories[0].password"]
    assert not report.rewritten
    assert path.read_text() == before


@pytest.mark.asyncio
async def test_a_broken_project_file_is_skipped_and_left_alone(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """One unreadable value must not abort the round, and must not write half a file."""
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    path = tmp_path / "kapot.yaml"
    await _write_project(path, public_a, generate_sops_key_pair())
    text = path.read_text().replace(
        "password: base64+age:", "password: base64+age:" + base64.b64encode(b"not-age").decode() + "#"
    )
    path.write_text(text)
    before = path.read_text()

    report = await rotate_project_file(path, private_a, private_b, dry_run=False)

    assert report.skipped is not None
    assert not report.rewritten
    assert path.read_text() == before


@pytest.mark.asyncio
async def test_a_pat_replacement_touches_only_the_repository_password(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """The second entry point on the same loop: the project key is recrypted, not replaced."""
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    project_pair = generate_sops_key_pair()
    path = tmp_path / "proefproject.yaml"
    await _write_project(path, public_a, project_pair)

    report = await rotate_project_file(
        path, private_a, private_b, current_pat="ghp_repository_token", new_pat="ghp_brand_new", dry_run=False
    )

    assert report.rewritten
    data = load_yaml_from_path(str(path))
    assert await decrypt_field(data["repositories"][0]["password"], private_b) == "ghp_brand_new"
    assert await decrypt_field(data["config"]["age-private-key"], private_b) == project_pair[0]
    repo_key = f"{path}#repositories[0].password"
    key_key = f"{path}#{PROJECT_FIELD_PRIVATE_KEY}"
    assert report.fingerprint_before.fields[key_key] == report.fingerprint_after.fields[key_key]
    assert report.fingerprint_before.fields[repo_key] != report.fingerprint_after.fields[repo_key]
    # Exactly the password, and named by the conversion rather than derived from the field
    # name: this list is what EXCUSES a hash from the content check, so a key on it would let
    # the project key drift unnoticed and a password missing from it would fail a good round.
    assert report.replaced == [repo_key]
    assert report.kept == []


@pytest.mark.asyncio
async def test_a_password_that_is_not_the_current_pat_is_recrypted_and_kept(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """The conditional replacement, at the field where the decision is made.

    Measured on the live repos: the 58 project passwords are uniform today, but the 67 ArgoCD
    secrets derived from them are not -- two carry another value on the same GitHub URL, so
    almost certainly an older token. Nothing makes the project files the half that stays
    uniform, and the round used to write the new PAT over whatever it could read. Now the value
    survives, the key half still happens, and the field is named with its path.
    """
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    path = tmp_path / "proefproject.yaml"
    await _write_project(path, public_a, generate_sops_key_pair())

    report = await rotate_project_file(
        path, private_a, private_b, current_pat="ghp_a_different_token", new_pat="ghp_brand_new", dry_run=False
    )

    assert report.rewritten
    data = load_yaml_from_path(str(path))
    # The value is untouched and the KEY moved anyway: those are two separate promises.
    assert await decrypt_field(data["repositories"][0]["password"], private_b) == "ghp_repository_token"
    assert await decrypt_field(data["repositories"][0]["password"], private_a) is None
    repo_key = f"{path}#repositories[0].password"
    assert report.replaced == []
    assert report.kept == [f"{repo_key}: the plaintext is not the current PAT, so it was left as it is"]
    assert report.fingerprint_before.fields[repo_key] == report.fingerprint_after.fields[repo_key]


@pytest.mark.asyncio
async def test_a_pat_round_without_the_current_pat_is_refused(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """Without something to compare against, "conditional" is the unconditional round renamed.

    A default of None that simply falls back to replacing everything would leave the old
    behaviour reachable from every caller that forgets the argument, and that is precisely how
    it got there.
    """
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    path = tmp_path / "proefproject.yaml"
    await _write_project(path, public_a, generate_sops_key_pair())
    before = path.read_text()

    with pytest.raises(ConversionFailed, match="needs the current PAT"):
        await rotate_project_file(path, private_a, private_b, new_pat="ghp_brand_new", dry_run=False)

    assert path.read_text() == before


@pytest.mark.asyncio
async def test_a_decrypted_project_key_is_refused_rather_than_written(
    tmp_path: Path, key_pair_a: tuple[str, str], key_pair_b: tuple[str, str]
) -> None:
    """The one check that stays hard: a rotation may never leave plaintext behind."""
    private_a, public_a = key_pair_a
    private_b, _public_b = key_pair_b
    path = tmp_path / "proefproject.yaml"
    await _write_project(path, public_a, generate_sops_key_pair())

    data = load_yaml_from_path(str(path))
    set_project_field(data, "repositories[0].password", "plain-text-token")

    # The guard is reached directly: the loop itself never produces this state, and the guard
    # exists for the input file that already carries it.
    assert plaintext_secrets(data) == ["repositories/0/password"]
