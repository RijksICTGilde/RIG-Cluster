"""Tests for the PAT round's loose-value pass: the three files that HOLD the shared token.

``LOOSE_VALUE_FILES`` has six entries and the key round re-encrypts all of them. The PAT round
asks a narrower question about the same list, and getting it wrong goes both ways:

* skip a carrier and every EXISTING project is right while the next NEW one gets the withdrawn
  token -- ``PROJECT_REPO_PASSWORD`` in ``opi/core/config.py`` is the default that reaches
  ``repositories[].password`` of every new project through ``project-template.yaml``. That
  failure surfaces at the next project creation, by which time the old token is long revoked;
* replace one too many and OPI loses its own git access -- the two configmaps and ``.env`` hold
  ``GIT_PROJECTS_SERVER_PASSWORD`` and ``GIT_ARGO_APPLICATIONS_PASSWORD``, the platform's
  credentials for its own three repositories, on that same platform key.

So the decision is measured on the decrypted value, with the GitHub shapes ``secret_scan``
already knows, instead of being settled by a hand-written list of three file names.
"""

from __future__ import annotations

import base64
import shutil
import sys
from pathlib import Path

import pytest
from opi.utils.age import BASE64_AGE_PREFIX, encrypt_age_content
from opi.utils.sops import generate_sops_key_pair

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from key_rotation import Fingerprint, decrypt_field, is_github_token, sha256_of, update_record  # noqa: E402
from project_rotation import classify_loose_values, run_loose_round  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="requires the age binary")

OLD_TOKEN = "ghp_" + "o" * 36
NEW_TOKEN = "ghp_" + "n" * 36
GIT_SERVER_PASSWORD = "a-forgejo-password"


async def _encrypted(value: str, public_key: str) -> str:
    block = await encrypt_age_content(value, public_key)
    return BASE64_AGE_PREFIX + base64.b64encode(block.encode()).decode()


async def _env_file(path: Path, public_key: str, values: dict[str, str]) -> Path:
    lines = [f"{name}={await _encrypted(value, public_key)}" for name, value in values.items()]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [
        ("ghp_" + "a" * 36, True),
        ("github_pat_" + "b" * 50, True),
        ("gho_" + "c" * 36, True),
        ("a-forgejo-password", False),
        ("", False),
        ("ghp_short", False),
    ],
)
def test_which_plaintexts_count_as_a_github_token(plaintext: str, expected: bool) -> None:
    """The one rule the pass hangs on, and it comes from the scanner's own rule set.

    A second spelling of these shapes next to ``secret_scan.RULES`` would drift the first time
    GitHub adds a prefix, and the drift would be silent in exactly the direction that matters:
    a token nobody replaces.
    """
    assert is_github_token(plaintext) is expected


@pytest.mark.asyncio
async def test_the_carrier_is_replaced_and_the_git_server_password_is_not(tmp_path: Path) -> None:
    """Both halves in one file, because that is how they really sit: same file, same key."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(
        tmp_path / "config.py",
        old_public,
        {"PROJECT_REPO_PASSWORD": OLD_TOKEN, "GIT_PROJECTS_SERVER_PASSWORD": GIT_SERVER_PASSWORD},
    )

    result = await classify_loose_values([path], old_private, new_private)
    await run_loose_round(result, new_public, NEW_TOKEN, dry_run=False)

    assert [entry.field_.name for entry in result.carriers] == ["PROJECT_REPO_PASSWORD"]
    assert [entry.field_.name for entry in result.others] == ["GIT_PROJECTS_SERVER_PASSWORD"]
    written = await classify_loose_values([path], old_private, new_private)
    values = {entry.field_.name: entry.plaintext for entry in written.carriers + written.others}
    assert values == {"PROJECT_REPO_PASSWORD": NEW_TOKEN, "GIT_PROJECTS_SERVER_PASSWORD": GIT_SERVER_PASSWORD}


@pytest.mark.asyncio
async def test_the_replaced_value_moves_to_the_new_key_as_well(tmp_path: Path) -> None:
    """One pass, not two: the value is re-encrypted for B in the same write that replaces it."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / "config.py", old_public, {"PROJECT_REPO_PASSWORD": OLD_TOKEN})

    result = await classify_loose_values([path], old_private, new_private)
    await run_loose_round(result, new_public, NEW_TOKEN, dry_run=False)

    value = path.read_text().split("=", 1)[1].strip()
    assert await decrypt_field(value, new_private) == NEW_TOKEN
    assert await decrypt_field(value, old_private) is None


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / "config.py", old_public, {"PROJECT_REPO_PASSWORD": OLD_TOKEN})
    before = path.read_text()

    result = await classify_loose_values([path], old_private, new_private)
    updates = await run_loose_round(result, new_public, NEW_TOKEN, dry_run=True)

    assert result.converted == [f"{path}#PROJECT_REPO_PASSWORD"]
    assert updates, "the dry run still has to say which record entries would move"
    assert path.read_text() == before


@pytest.mark.asyncio
async def test_a_second_round_leaves_the_value_where_it_is(tmp_path: Path) -> None:
    """A value that already holds this token keeps its ciphertext, so the diff stays empty.

    Re-encrypting it would be harmless for the content and wrong for the operator: a second run
    that rewrites files says "something happened here" when nothing did.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / "config.py", old_public, {"PROJECT_REPO_PASSWORD": OLD_TOKEN})
    first = await classify_loose_values([path], old_private, new_private)
    await run_loose_round(first, new_public, NEW_TOKEN, dry_run=False)
    after_first = path.read_text()

    second = await classify_loose_values([path], old_private, new_private)
    await run_loose_round(second, new_public, NEW_TOKEN, dry_run=False)

    assert second.converted == []
    assert second.already == [f"{path}#PROJECT_REPO_PASSWORD"]
    assert path.read_text() == after_first


@pytest.mark.asyncio
async def test_a_value_that_opens_with_neither_key_is_a_finding(tmp_path: Path) -> None:
    """Not a skip: the round's exit code has to say so, or a missed file reads as success."""
    old_private, _old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    _stranger_private, stranger_public = generate_sops_key_pair()
    path = await _env_file(tmp_path / "config.py", stranger_public, {"PROJECT_REPO_PASSWORD": OLD_TOKEN})

    result = await classify_loose_values([path], old_private, new_private)

    assert result.closed == [f"{path}#PROJECT_REPO_PASSWORD"]
    assert result.carriers == []


@pytest.mark.asyncio
async def test_the_repo_record_is_corrected_for_exactly_the_replaced_fields(tmp_path: Path) -> None:
    """Without this the next ``--verify`` objects to a replacement it asked for itself.

    The record of this repo holds the sha256 of the PLAINTEXT per field, and the PAT round is the
    one round where a plaintext is supposed to change. The field next to it must NOT move, which
    is why the correction is per name rather than a fresh recording of the whole file.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    path = await _env_file(
        tmp_path / "config.py",
        old_public,
        {"PROJECT_REPO_PASSWORD": OLD_TOKEN, "GIT_PROJECTS_SERVER_PASSWORD": GIT_SERVER_PASSWORD},
    )
    record = tmp_path / "fingerprint.json"
    before = await classify_loose_values([path], old_private, new_private)
    recorded = Fingerprint()
    for entry in before.carriers + before.others:
        recorded.set(entry.name, sha256_of(entry.plaintext))
    recorded.save(record)

    updates = await run_loose_round(before, new_public, NEW_TOKEN, dry_run=False)
    assert update_record(record, updates) == []

    written = Fingerprint.load(record)
    assert written.fields[f"{path}#PROJECT_REPO_PASSWORD"] == sha256_of(NEW_TOKEN)
    assert written.fields[f"{path}#GIT_PROJECTS_SERVER_PASSWORD"] == sha256_of(GIT_SERVER_PASSWORD)


def test_a_record_that_does_not_know_a_field_is_refused_rather_than_extended(tmp_path: Path) -> None:
    """A name the record does not hold means the round wrote outside what it records.

    Adding it would turn the record from a check into a tally -- the same trap
    ``replace_record`` exists for -- and it would hide a coverage gap behind a growing file.
    """
    record = tmp_path / "fingerprint.json"
    Fingerprint(fields={"a#known": "0" * 64}).save(record)

    objections = update_record(record, {"a#unknown": "1" * 64})

    assert len(objections) == 1
    assert "a#unknown" in objections[0]
    assert Fingerprint.load(record).fields == {"a#known": "0" * 64}
