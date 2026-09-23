"""The engine under the key rotation: one loop, four places, one fingerprint.

The whole story, with the measurements, is in ``features/sops-sleutel-vervangen.md``.

There is a single operation under every place that is not a SOPS file, and it lives here once:

    read field -> decrypt with A -> [keep value OR replace it] -> encrypt for B -> write
                                             ^              ^
                                          recrypt       new PAT

``convert_value()`` is that loop. Without ``new_plaintext`` it is a recrypt to B; with one
it is a replacement plus a recrypt. Two entry points sit on it: the key rotation
(``rotate-project-keys.py``) and the PAT replacement (``replace-git-pat.py``).

**Why by recipient and not by filename.** ``sops_files_for()`` selects on the recipient in
the metadata, so a file encrypted for any other key is not touched. The alternative is a path
exclusion list, and that silently falls behind the moment a file is added. This is not
hypothetical: the tree held a practice key in ``sops-sandbox/`` with two files of its own until
this rotation removed it, and the sandbox and developer keys are still separate keys.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# Make ``opi`` importable regardless of the working directory: the package lives in
# operations-manager/python, a sibling of this scripts/ directory.
_OPI_ROOT = Path(__file__).resolve().parents[1] / "operations-manager" / "python"
if str(_OPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPI_ROOT))

from opi.core.project_schema import (  # noqa: E402  (after the sys.path bootstrap above)
    find_plaintext_secret_violations,
    validate_project_schema,
)
from opi.manager.project_validation import validate_project_structure  # noqa: E402
from opi.utils.age import (  # noqa: E402
    BASE64_AGE_PREFIX,
    _encrypt_with_age_and_base64encode_as_prefixed_string,
    decrypt_age_content,
    encrypt_age_content,
    is_age_encrypted,
)
from opi.utils.sops import _decrypt_sops_with_key  # noqa: E402
from opi.utils.yaml_util import load_yaml_from_path, save_yaml_to_path  # noqa: E402
from ruamel.yaml.scalarstring import LiteralScalarString  # noqa: E402

AGE_KEY_MARKER = "AGE-SECRET-KEY-"

#: The two project-file fields that hang off the PLATFORM key. Measured across the 45
#: project files: only these two open with the platform key. ``api-key``,
#: ``keycloak[].password``, ``user-env-vars``, ``configuration``, the attachments and
#: ``registries[].password`` hang off the project's own key, which itself sits encrypted in
#: ``config.age-private-key``. A project where only the first field was converted can no
#: longer reach its own repository.
PROJECT_FIELD_PRIVATE_KEY = "config.age-private-key"
PROJECT_FIELD_REPO_PASSWORD = "repositories[{index}].password"  # noqa: S105 - a field path, not a password

#: Env lines carrying an encrypted value: ``KEY=base64+age:<base64>``. The value is base64
#: and therefore always a single line, which is what makes a line-scoped replacement safe
#: here -- unlike a project file, where ``age-private-key`` is a multi-line block scalar and
#: text replacement silently produces the wrong indentation.
_ENV_LINE = re.compile(rf"^(?P<key>[A-Z0-9_]+)=(?P<value>{re.escape(BASE64_AGE_PREFIX)}[A-Za-z0-9+/=]+)\s*$")


class Form(StrEnum):
    """The storage form of an encrypted value. It has to survive the conversion.

    ``BLOCK`` is the ASCII-armored AGE block: multi-line, a literal block scalar in YAML.
    ``BASE64`` is that same block base64-encoded once more behind ``base64+age:``, a single
    line, which is what lets it fit in a ``KEY=value`` env line. A field that switches form
    is a content change, and a rotation must not make one.
    """

    BLOCK = "age-block"
    BASE64 = "base64+age"


class MissingKey(RuntimeError):
    """A key file is absent, or holds no AGE key. Always names the path."""


class ConversionFailed(RuntimeError):
    """A field does not open with the old key, or does not open with the new one afterwards."""


# keys


def read_key(path: str | Path) -> str:
    """Take the private key out of an ``age-keygen`` file.

    Searching for the marker is sturdier than grabbing the third line: the latter breaks
    the moment a comment line is added. The message always names the path, because the
    ordinary mistake here is that the file lives somewhere else.
    """
    path = Path(path)
    if not path.is_file():
        raise MissingKey(f"key file does not exist: {path}")
    for line in path.read_text().splitlines():
        if line.strip().startswith(AGE_KEY_MARKER):
            return line.strip()
    raise MissingKey(f"no {AGE_KEY_MARKER} line in {path}")


def public_key_of(private_key: str) -> str:
    """Derive the public half with ``age-keygen -y``.

    The tools never ask for the public key: it is derivable, and a hand-typed second half
    is an opportunity to encrypt for the wrong key.
    """
    # S603/S607: a fixed argument list with a bare program name, resolved through PATH. The
    # tools in this repo (age, age-keygen, sops, kubectl) are all invoked that way; the key
    # goes in over stdin precisely so it cannot reach the argument list.
    process = subprocess.run(
        ["age-keygen", "-y"],  # noqa: S607
        input=private_key,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise MissingKey(f"age-keygen could not derive the public key: {process.stderr.strip()}")
    return process.stdout.strip()


def ask_for_path(question: str, default: str | Path, *, reader: Any = input) -> Path:
    """Ask for a path with a default, and refuse when the file is not there.

    Keys are never taken as an argument. A key on the command line lands in the shell
    history, in the process table and in every log that records the command; a path to a
    file in the untracked ``security/`` does not.
    """
    answer = reader(f"{question} [{default}]: ").strip() or str(default)
    path = Path(answer)
    if not path.is_file():
        raise MissingKey(f"file does not exist: {path}")
    return path


# the one loop


def form_of(value: object) -> Form | None:
    """The storage form of a value, or None when nothing encrypted is in it."""
    if not isinstance(value, str) or not value:
        return None
    stripped = value.strip()
    if stripped.startswith(BASE64_AGE_PREFIX):
        return Form.BASE64
    if is_age_encrypted(stripped):
        return Form.BLOCK
    return None


def _to_block(value: str, form: Form) -> str:
    """Peel the storage form back to the armored AGE block that ``age -d`` accepts."""
    if form is Form.BASE64:
        return base64.b64decode(value.strip()[len(BASE64_AGE_PREFIX) :]).decode()
    return value.strip()


def sha256_of(plaintext: str) -> str:
    """The fingerprint of one field: sha256 of the plaintext, hex."""
    return sha256(plaintext.encode()).hexdigest()


@dataclass(frozen=True)
class Conversion:
    """The result of one conversion. Holds the new ciphertext, never the plaintext."""

    new_value: str
    sha256_before: str
    sha256_after: str
    replaced: bool

    @property
    def content_unchanged(self) -> bool:
        """Whether the plaintext stayed the same. For a replacement this should be False."""
        return self.sha256_before == self.sha256_after


async def convert_value(
    value: str,
    old_private_key: str,
    new_public_key: str,
    *,
    new_plaintext: str | None = None,
) -> Conversion:
    """THE loop: read, decrypt with A, keep or replace, encrypt for B, hand it back.

    Without ``new_plaintext`` this is a recrypt: the plaintext stays the same and the hash
    before and after is identical. With ``new_plaintext`` it is a replacement plus a
    recrypt, and then the hash is supposed to differ -- at exactly those fields and nowhere
    else.

    The storage form carries over: a ``base64+age:`` value comes back as ``base64+age:``
    and an armored block as an armored block. A field that switches form is itself a change.
    """
    form = form_of(value)
    if form is None:
        raise ConversionFailed("value is not encrypted and should not reach this loop")

    plain_before = await decrypt_age_content(_to_block(value, form), old_private_key)
    plain_after = new_plaintext if new_plaintext is not None else plain_before

    if form is Form.BASE64:
        new_value = await _encrypt_with_age_and_base64encode_as_prefixed_string(plain_after, new_public_key)
    else:
        new_value = await encrypt_age_content(plain_after, new_public_key)

    return Conversion(
        new_value=new_value,
        sha256_before=sha256_of(plain_before),
        sha256_after=sha256_of(plain_after),
        replaced=new_plaintext is not None,
    )


async def decrypt_field(value: str, private_key: str) -> str | None:
    """The plaintext of a field, or None when this key does not open it.

    None rather than an exception: both outcomes are a normal answer here. The final check
    specifically wants to KNOW that the old key fails, and a verification round has to be
    able to name every closed field instead of aborting on the first one.
    """
    form = form_of(value)
    if form is None:
        return None
    try:
        return await decrypt_age_content(_to_block(value, form), private_key)
    except Exception:  # age, base64 and the wrapper each raise their own type
        return None


async def opens_with(value: str, private_key: str) -> bool:
    """Whether a field can be decrypted with this key. The core of the final check."""
    return await decrypt_field(value, private_key) is not None


# fingerprint


@dataclass
class Fingerprint:
    """A path, a field name and a hash per encrypted field. No secrets.

    This is the product that lets you show afterwards that nothing changed but the key. The
    number of fields before and after has to match: a field that silently disappears during
    a YAML round trip is caught here, and that is exactly the failure text replacement makes
    without crashing.
    """

    fields: dict[str, str] = field(default_factory=dict)
    created: str = ""

    def set(self, key: str, hash_hex: str) -> None:
        self.fields[key] = hash_hex

    def save(self, path: str | Path) -> None:
        self.created = self.created or datetime.now(UTC).isoformat()
        Path(path).write_text(json.dumps({"created": self.created, "fields": self.fields}, indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: str | Path) -> Fingerprint:
        raw = json.loads(Path(path).read_text())
        return cls(fields=dict(raw["fields"]), created=str(raw.get("created", "")))

    def compare(self, other: Fingerprint, *, replaced: Iterable[str] = ()) -> list[str]:
        """Every deviation between two fingerprints, as readable lines.

        ``replaced`` names the fields where a different hash is the intent (the PAT). Those
        then have to actually differ: a replacement that did not land is as much a failure as
        a value that shifted without being asked to.
        """
        objections: list[str] = []
        replaced = set(replaced)
        objections.extend(f"field disappeared: {gone}" for gone in sorted(set(self.fields) - set(other.fields)))
        objections.extend(f"field appeared: {added}" for added in sorted(set(other.fields) - set(self.fields)))
        for key in sorted(set(self.fields) & set(other.fields)):
            same = self.fields[key] == other.fields[key]
            if key in replaced and same:
                objections.append(f"replacement did not land, content unchanged: {key}")
            elif key not in replaced and not same:
                objections.append(f"content changed: {key}")
        return objections


# place 1: SOPS files


def sops_files(tree: str | Path) -> list[Path]:
    """Every file with a ``sops:`` metadata block, whatever it is called.

    Matching on the name would miss half of them: next to the ``*.sops.yaml`` files the tree
    holds two ``operations-manager-env-secrets.yaml`` without that suffix.
    """
    found: list[Path] = []
    for path in sorted(Path(tree).rglob("*.yaml")):
        if ".git" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r"^sops:", content, re.MULTILINE):
            found.append(path)
    return found


def sops_recipients(path: str | Path) -> list[str]:
    """The AGE recipients from one file's SOPS metadata."""
    content = Path(path).read_text(encoding="utf-8")
    return re.findall(r"^\s*-?\s*recipient:\s*(age1[a-z0-9]+)\s*$", content, re.MULTILINE)


def sops_files_for(tree: str | Path, public_key: str) -> list[Path]:
    """The SOPS files that sit on this recipient -- and no others."""
    return [path for path in sops_files(tree) if public_key in sops_recipients(path)]


def sops_plaintext(path: str | Path, private_key: str) -> str | None:
    """The decrypted content of a SOPS file, or None when the key does not fit.

    Uses ``opi.utils.sops._decrypt_sops_with_key``: exactly this function, with an explicit
    key instead of the one from the environment, already lives there. A second copy
    alongside it quietly grows different behaviour.
    """
    return _decrypt_sops_with_key(str(path), private_key)


def sops_rotate(path: str | Path, old_public_key: str, new_public_key: str, old_private_key: str) -> None:
    """``sops rotate -i --add-age B --rm-age A``: new data key, A off, in one move.

    No dual recipients. An overlap phase keeps A valid on purpose and adds a second round to
    take it off again later, and that round can be forgotten.
    """
    process = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "sops",
            "--disable-version-check",
            "rotate",
            "-i",
            "--add-age",
            new_public_key,
            "--rm-age",
            old_public_key,
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SOPS_AGE_KEY": old_private_key},
    )
    if process.returncode != 0:
        raise ConversionFailed(f"sops rotate failed on {path}: {process.stderr.strip()}")


# places 2 and 3: loose base64+age: values in an env line


@dataclass(frozen=True)
class EnvField:
    """One ``KEY=base64+age:...`` line, with the line number needed to write it back."""

    path: Path
    key: str
    value: str
    line_number: int

    @property
    def name(self) -> str:
        return self.key


def env_fields(path: str | Path) -> list[EnvField]:
    """Every env line with a ``base64+age:`` value in a text file.

    Works on text and not on YAML, the two ``configmap.yaml`` included: there the env
    content sits in a literal block scalar, and a YAML round trip would re-emit that whole
    block. The value is base64 and therefore always a single line, so a line-scoped
    replacement touches exactly that one field.
    """
    path = Path(path)
    found: list[EnvField] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = _ENV_LINE.match(line.strip())
        if match:
            found.append(EnvField(path=path, key=match.group("key"), value=match.group("value"), line_number=number))
    return found


def write_env_value(path: str | Path, line_number: int, old_value: str, new_value: str) -> None:
    """Replace exactly one value on exactly one line, leaving the indentation intact.

    Written through a temporary file in the same directory and moved into place with
    ``os.replace``, for the same reason the YAML writer does it: a torn ``configmap.yaml`` or
    ``.env`` is worse than an unconverted one, and it would be discovered by a deployment rather
    than by this tool.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    index = line_number - 1
    if old_value not in lines[index]:
        raise ConversionFailed(f"{path}:{line_number} no longer holds the expected value")
    lines[index] = lines[index].replace(old_value, new_value)

    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as target:
            target.write("".join(lines))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# place 4: project files


def project_fields(data: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(field name, ciphertext)`` pairs in a project file that hang off the platform key."""
    fields: list[tuple[str, str]] = []
    private_key = (data.get("config") or {}).get("age-private-key")
    if isinstance(private_key, str) and form_of(private_key) is not None:
        fields.append((PROJECT_FIELD_PRIVATE_KEY, private_key))
    for index, repository in enumerate(data.get("repositories") or []):
        password = repository.get("password") if isinstance(repository, dict) else None
        if isinstance(password, str) and form_of(password) is not None:
            fields.append((PROJECT_FIELD_REPO_PASSWORD.format(index=index), password))
    return fields


def set_project_field(data: dict[str, Any], field_name: str, new_value: str) -> None:
    """Write one converted value back into the loaded tree.

    A multi-line value goes back as a ``LiteralScalarString``. A plain ``str`` with newlines
    in it is valid YAML but produces a different file; the canonical writer in ``yaml_util``
    does catch that, but only as long as the value does not pass through a JSON round trip on
    the way. Being explicit is cheaper here than depending on it.
    """
    to_write: Any = LiteralScalarString(new_value) if "\n" in new_value else new_value
    if field_name == PROJECT_FIELD_PRIVATE_KEY:
        data["config"]["age-private-key"] = to_write
        return
    match = re.fullmatch(r"repositories\[(\d+)\]\.password", field_name)
    if not match:
        raise ConversionFailed(f"unknown project field: {field_name}")
    data["repositories"][int(match.group(1))]["password"] = to_write


async def validate_project_data(data: dict[str, Any]) -> str | None:
    """The two validations ``ProjectStore`` runs before every write, in the same order.

    Returns None when clean, otherwise the message. The caller decides what a message
    means: 44 of the 45 measured project files do not satisfy the CURRENT schema (2.8)
    because they are older and only get migrated when OPI reads them. That pre-existing
    drift must not block a key rotation; drift that appears AFTER the conversion must.
    """
    try:
        validate_project_schema(data)
        await validate_project_structure(data)
    except Exception as e:  # the two validators each raise their own type
        return str(e).replace("\n", " ")[:300]
    return None


def plaintext_secrets(data: dict[str, Any]) -> list[str]:
    """Fields that must be encrypted but hold plain text.

    This is the one check that stays hard even in the face of pre-existing drift: it is the
    failure where a rotation would leave a secret in plain form in git.
    """
    return find_plaintext_secret_violations(data)


@dataclass
class ProjectRound:
    """What happened to one project file, in terms that fit on a screen."""

    path: Path
    fields: list[str] = field(default_factory=list)
    skipped: str | None = None
    fingerprint_before: Fingerprint = field(default_factory=Fingerprint)
    fingerprint_after: Fingerprint = field(default_factory=Fingerprint)
    rewritten: bool = False
    validation_was_already_red: str | None = None


def nothing_to_do(total: int, on_new_key: int, passwords: int, *, new_pat: str | None) -> str:
    """Why this file needs no work, in the terms of the round that is actually running.

    "Already converted" used to cover both rounds; in the PAT round it reads as reassurance
    while the file may still hold the old token. ``broken()`` judges the two sets separately.
    """
    if new_pat is None:
        return f"already converted ({on_new_key} of {total} fields sit on the new key)"
    if not passwords:
        return "no repository password to replace"
    return f"the repository password already holds this PAT ({on_new_key} of {total} fields sit on the new key)"


async def rotate_project_file(
    path: str | Path,
    old_private_key: str,
    new_private_key: str,
    *,
    new_public_key: str | None = None,
    new_pat: str | None = None,
    dry_run: bool = True,
) -> ProjectRound:
    """Convert one project file's two platform fields, or explain why not.

    Idempotent, and that is measured rather than assumed: a field that does not open with A
    but does open with B already sits on the new key, and a field that opens with neither is
    unreadable. That distinction is why the NEW private key comes in here and not just its
    public half -- without B, "already done" cannot be told apart from "broken", and a second
    round would then silently skip good files.

    A file with an unreadable value is skipped with a message and NOT half written.

    ``new_pat`` replaces ``repositories[].password``; ``config.age-private-key`` is always
    only re-encrypted.

    The worklist therefore cannot be settled by the key alone: the documented order runs the
    key round first, so by then every password already sits on B while still holding the OLD
    token, and a gate on "does this still open with A?" makes that second round a silent no-op.
    """
    path = Path(path)
    round_report = ProjectRound(path=path)
    new_public_key = new_public_key or public_key_of(new_private_key)

    data = load_yaml_from_path(str(path))
    if not isinstance(data, dict):
        round_report.skipped = "not a readable project file"
        return round_report

    fields = project_fields(data)
    if not fields:
        round_report.skipped = "no encrypted platform fields in this file"
        return round_report

    todo: list[tuple[str, str, str]] = []
    on_new_key = 0
    passwords = 0
    for field_name, value in fields:
        is_password = field_name != PROJECT_FIELD_PRIVATE_KEY
        if is_password:
            passwords += 1
        if await opens_with(value, old_private_key):
            todo.append((field_name, value, old_private_key))
            continue
        plain = await decrypt_field(value, new_private_key)
        if plain is None:
            round_report.skipped = f"{field_name} opens with neither key"
            return round_report
        on_new_key += 1
        if new_pat is not None and is_password and plain != new_pat:
            todo.append((field_name, value, new_private_key))

    if not todo:
        round_report.skipped = nothing_to_do(len(fields), on_new_key, passwords, new_pat=new_pat)
        return round_report

    round_report.validation_was_already_red = await validate_project_data(data)

    for field_name, value, opening_key in todo:
        replacement = new_pat if (new_pat is not None and field_name != PROJECT_FIELD_PRIVATE_KEY) else None
        try:
            conversion = await convert_value(value, opening_key, new_public_key, new_plaintext=replacement)
        except Exception as e:  # age and base64 each raise their own type
            round_report.skipped = f"{field_name} does not open: {e}"
            return round_report
        key = f"{path}#{field_name}"
        round_report.fingerprint_before.set(key, conversion.sha256_before)
        round_report.fingerprint_after.set(key, conversion.sha256_after)
        round_report.fields.append(field_name)
        set_project_field(data, field_name, conversion.new_value)

    leaked = plaintext_secrets(data)
    if leaked:
        round_report.skipped = f"conversion would leave plaintext secrets: {', '.join(leaked)}"
        round_report.fields.clear()
        return round_report

    after = await validate_project_data(data)
    if after and not round_report.validation_was_already_red:
        round_report.skipped = f"conversion makes the file invalid: {after}"
        round_report.fields.clear()
        return round_report

    if not dry_run:
        # save_yaml_to_path and not write_text: it dumps to a temporary file and moves it into
        # place, so a reader sees either the old file or the complete new one. A torn project
        # file is the one outcome worse than a skipped one.
        save_yaml_to_path(str(path), data)
        round_report.rewritten = True
    return round_report


# the final check


@dataclass
class FinalCheck:
    """The result of ``--assert-old-key-dead`` over all four places."""

    still_opens_with_old: list[str] = field(default_factory=list)
    does_not_open_with_new: list[str] = field(default_factory=list)
    counted: int = 0
    expected: int | None = None

    @property
    def clean(self) -> bool:
        return not self.still_opens_with_old and not self.does_not_open_with_new and self.count_matches

    @property
    def count_matches(self) -> bool:
        return self.expected is None or self.counted == self.expected

    def lines(self) -> list[str]:
        out = [f"{self.counted} fields checked"]
        if self.expected is not None and not self.count_matches:
            out.append(
                f"FAIL count differs from the fingerprint: {self.counted} now, {self.expected} before the conversion"
            )
        out.extend(f"FAIL STILL opens with the old key: {name}" for name in self.still_opens_with_old)
        out.extend(f"FAIL does NOT open with the new key: {name}" for name in self.does_not_open_with_new)
        if self.clean:
            out.append("CLEAN the old key opens nothing, the new key opens everything")
        return out


def check_sops_file(path: Path, old_private_key: str, new_private_key: str, check: FinalCheck) -> None:
    """One SOPS file: A must fail, B must succeed."""
    check.counted += 1
    name = str(path)
    if sops_plaintext(path, old_private_key) is not None:
        check.still_opens_with_old.append(name)
    if sops_plaintext(path, new_private_key) is None:
        check.does_not_open_with_new.append(name)


async def check_value(name: str, value: str, old_private_key: str, new_private_key: str, check: FinalCheck) -> None:
    """One loose value: A must fail, B must succeed."""
    check.counted += 1
    if await opens_with(value, old_private_key):
        check.still_opens_with_old.append(name)
    if not await opens_with(value, new_private_key):
        check.does_not_open_with_new.append(name)


if __name__ == "__main__":
    print(__doc__)
    print("This is the engine, not a tool. The entry points sit next to it:")
    print("  rotate-sops-key.py, rotate-project-keys.py, replace-git-pat.py, set-sops-key-secret.py")
    raise SystemExit(1)
