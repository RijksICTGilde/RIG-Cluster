"""The engine under the key rotation: one loop, five places, one fingerprint.

The whole story, with the measurements, is in ``features/sops-sleutel-roteren.md``.

There is a single operation under every place that is not a SOPS file, and it exists once:

    read field -> decrypt with A -> [keep value OR replace it] -> encrypt for B -> write
                                             ^              ^
                                          recrypt       new PAT

``convert_value()`` is that loop. It lives in ``opi.utils.key_rotation`` and is imported
here, so the API route that replaces a single project key runs the same loop as these
scripts. Without ``new_plaintext`` it is a recrypt to B; with one it is a replacement plus a
recrypt. Two entry points sit on it: the key rotation (``rotate-project-keys.py``) and the
PAT replacement (``replace-git-pat.py``).

**Which of the two branches a field takes is decided on its PLAINTEXT.** The PAT round is
given the token it replaces as well as the one it writes, and replaces only where the two
match; every other value takes the recrypt branch with its content untouched and is named in
the report. Measured on the live repos, that is not a corner case: of the 67 ArgoCD repository
secrets two carry a value the other 65 do not, and for one of those two the project file says
something else again -- drift that was already there. An unconditional round writes over all
three of those without a word.

**Why by recipient and not by filename.** ``sops_files_for()`` selects on the recipient in
the metadata, so a file encrypted for any other key is not touched. The alternative is a path
exclusion list, and that silently falls behind the moment a file is added. This is not
hypothetical: the tree held a practice key in ``sops-sandbox/`` with two files of its own until
this rotation removed it, and the sandbox and developer keys are still separate keys.

**The loose values do need a list, so they get a guard.** ``files_with_ciphertext()`` is the
inventory that list is checked against; ``coverage_gaps()`` in the entry point is that check,
and says why a list is unavoidable there.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# Make ``opi`` importable regardless of the working directory: the package lives in
# operations-manager/python, a sibling of this scripts/ directory. The second entry is this
# directory itself, for the scanner module next door.
_OPI_ROOT = Path(__file__).resolve().parents[1] / "operations-manager" / "python"
if str(_OPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPI_ROOT))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from opi.core.cluster_config import (  # noqa: E402  (after the sys.path bootstrap above)
    CLUSTER_CONFIG,
    get_cluster_config,
)
from opi.core.project_schema import (  # noqa: E402  (after the sys.path bootstrap above)
    find_plaintext_secret_violations,
    validate_project_schema,
)
from opi.manager.project_validation import validate_project_structure  # noqa: E402
from opi.utils.age import (  # noqa: E402
    BASE64_AGE_PREFIX,
    ensure_base64_age_sync,
    is_age_encrypted,
    parse_password_with_prefix,
)

# De lus zelf staat in OPI, zodat de API-route en deze scripts er één exemplaar van delen.
# Drie namen worden hier niet gebruikt maar wel uit deze module geïmporteerd door de
# rondes ernaast en door de toetsen: die kennen dit bestand als de ingang.
from opi.utils.key_rotation import (  # noqa: E402
    Conversion,  # noqa: F401
    ConversionFailed,
    Form,  # noqa: F401
    convert_value,
    decrypt_field,
    form_of,
    opens_with,
    sha256_of,
    to_age_block,
)
from opi.utils.sops import decrypt_sops_with_key  # noqa: E402
from opi.utils.yaml_util import load_yaml_from_path, save_yaml_to_path  # noqa: E402
from ruamel.yaml.scalarstring import LiteralScalarString  # noqa: E402

# "which files does git track, and which are worth reading" is the scanner's question and it is
# answered there. A second copy here would drift away from the one CI runs.
from secret_scan import RULES, skip_reason, tracked_files  # type: ignore[reportMissingImports]  # noqa: E402

#: The repository root. Every path this module resolves outside of an argument -- the loose
#: value files below -- hangs off it.
REPO = Path(__file__).resolve().parents[1]

#: Worktrees live INSIDE this repo, under this directory. Every walk here skips it along with
#: ``.git``: a worktree is a second checkout of the same files, so counting it means finding the
#: same secret several times and, worse, rewriting it in a working copy that is not yours.
WORKTREE_DIR = ".claude"

AGE_KEY_MARKER = "AGE-SECRET-KEY-"


# KEK per cluster: zie features/sops-sleutel-roteren.md.
def kek_file_for(cluster: str) -> Path:
    """Het KEK-bestand van dit cluster."""
    return REPO / "security" / f"project-kek-{cluster}.txt"


def kek_fingerprint_for(cluster: str) -> Path:
    """Het verslag van de KEK-ronde van dit cluster."""
    return REPO / "security" / f"project-kek-fingerprint-{cluster}.json"


def selected_cluster() -> str:
    """Het gekozen cluster uit ``CLUSTER_TYPE``.

    Alleen gezet binnen ``task``, dat ``.env-taskfile-current`` van ``task select-cluster``
    zelf inleest. In een gewone shell is dit leeg en noemt de weigering ``--cluster``.
    """
    return os.environ.get("CLUSTER_TYPE", "").strip()


def cluster_refusal(cluster: str) -> str | None:
    """De foutregel als dit geen cluster uit ``CLUSTER_CONFIG`` is, anders None."""
    if cluster in CLUSTER_CONFIG:
        return None
    known = ", ".join(sorted(CLUSTER_CONFIG))
    return (
        f"unknown cluster {cluster!r}; pass --cluster or run 'task select-cluster' so CLUSTER_TYPE "
        f"is set. Known clusters: {known}"
    )


def opi_namespace_for(cluster: str) -> str:
    """De platformnamespace waarin OPI op dit cluster draait, uit ``CLUSTER_CONFIG``.

    Uit de configuratie en niet uit een clusterbrede zoekopdracht op de deploymentnaam: die
    naam kiest een tenant, en staging en productie delen de API-server, dus een zoekopdracht
    levert daar de namespaces van BEIDE omgevingen op.
    """
    return get_cluster_config(cluster)["namespace"]


#: The GitHub token shapes, taken from the scanner's rule set rather than spelled out a second
#: time here. What the PAT round decides with them is in ``is_github_token``.
GITHUB_TOKEN_RULES = tuple(pattern for kind, pattern, _hint in RULES if kind.startswith("github"))

#: The two project-file fields that hang off the PLATFORM key. Measured across the 53
#: project files: only these two open with the platform key. ``api-key``,
#: ``keycloak[].password``, ``user-env-vars``, ``configuration``, the attachments and
#: ``registries[].password`` hang off the project's own key, which itself sits encrypted in
#: ``config.age-private-key``. A project where only the first field was converted can no
#: longer reach its own repository.
PROJECT_FIELD_PRIVATE_KEY = "config.age-private-key"
PROJECT_FIELD_REPO_PASSWORD = "repositories[{index}].password"  # noqa: S105 - a field path, not a password

#: A ``base64+age:<base64>`` value wherever it sits on a line. NOT anchored to ``KEY=value``:
#: the same platform-keyed value also stands in a Python literal
#: (``PROJECT_REPO_PASSWORD: str = "base64+age:..."``) and in a dict entry
#: (``"password": "base64+age:..."``), and an env-shaped pattern walked straight past both.
#: The value is base64 and therefore always a single line, which is what makes a line-scoped
#: replacement safe here -- unlike a project file, where ``age-private-key`` is a multi-line
#: block scalar and text replacement silently produces the wrong indentation.
_LOOSE_VALUE = re.compile(rf"(?P<value>{re.escape(BASE64_AGE_PREFIX)}[A-Za-z0-9+/=]{{20,}})")

#: The name a loose value goes under: the first identifier on its line. That is the env key in
#: ``GIT_PROJECTS_SERVER_PASSWORD=...``, the setting in ``PROJECT_REPO_PASSWORD: str = "..."``
#: and the dict key in ``"password": "..."``.
_NAME_BEFORE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

#: The ASCII armor around an AGE file, as it stands in a text file. The body may be indented
#: (a YAML block scalar), so it is de-indented before anything is decoded.
_ARMOR = re.compile(
    r"-----BEGIN AGE ENCRYPTED FILE-----[ \t]*\n"
    r"(?P<body>(?:[ \t]*[A-Za-z0-9+/=]+[ \t]*\n)+)"
    r"[ \t]*-----END AGE ENCRYPTED FILE-----"
)


class MissingKey(RuntimeError):
    """A key file is absent, or holds no AGE key. Always names the path."""


class KeyFileTooOpen(MissingKey):
    """Een sleutel- of tokenbestand is voor groep of anderen leesbaar of schrijfbaar.

    Een subclass van ``MissingKey`` zodat foutmeldingen-entry's een sleutelprobleem in één regel rapporteren, niet in een traceback.
    """


class KeyExists(RuntimeError):
    """A new key was about to be written where a file already sits. Always names the path."""


# keys


def require_private_file(path: str | Path) -> None:
    """Weigeren als een sleutel- of tokenbestand voor groep of anderen leesbaar is.

    De regel die ssh op een privésleutel toepast: te ruim is een bevinding nu, niet een verrassing later.
    """
    path = Path(path)
    if os.name != "posix":
        print(f"(POSIX-rechten niet van toepassing op dit platform; {path} niet gecontroleerd)")
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise KeyFileTooOpen(
            f"{path} is leesbaar of schrijfbaar buiten de eigenaar (mode {mode:04o}): chmod 600 {path}"
        )


def read_key(path: str | Path) -> str:
    """Take the private key out of an ``age-keygen`` file.

    Searching for the marker is sturdier than grabbing the third line: the latter breaks
    the moment a comment line is added. The message always names the path, because the
    ordinary mistake here is that the file lives somewhere else.
    """
    path = Path(path)
    if not path.is_file():
        raise MissingKey(f"key file does not exist: {path}")
    require_private_file(path)
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


def generate_key(path: Path) -> Path:
    """Make a new AGE key at ``path`` with ``age-keygen``, and never on top of a file.

    Landing on a key file destroys the only copy of that key. ``age-keygen -o`` opens with
    O_EXCL and refuses that itself -- measured -- but the words are worth owning, because the
    default answer in ``ask_for_new_key`` IS ``security/key.txt``: "refusing to overwrite an
    existing file" says why it matters where "failed to open output file" reads as a breakdown.
    """
    if path.exists():
        raise KeyExists(f"refusing to overwrite an existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # S603/S607: a bare program name resolved through PATH, as in ``public_key_of``. The
    # key is written to the file by age-keygen itself, so it never reaches the argument list.
    process = subprocess.run(  # noqa: S603
        ["age-keygen", "-o", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise MissingKey(f"age-keygen could not create a key at {path}: {process.stderr.strip()}")
    return path


def ask_for_new_key(question: str, default: str | Path, *, reader: Any = input) -> Path:
    """Ask where the new key goes and make it there, so step 1 is one command."""
    answer = reader(f"{question} [{default}]: ").strip() or str(default)
    return generate_key(Path(answer))


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


def content_drift(previous: Fingerprint, current: Fingerprint, replaced: Iterable[str] = ()) -> list[str]:
    """Where two records disagree about a field they BOTH hold.

    Deliberately not ``compare()`` over the whole record. That one also objects to a field
    appearing or disappearing, and here both are the documented path rather than a finding: a
    round may strand on an unreadable file and be run again once it is repaired, and the field
    that could not be measured is then new to the record. A project that has since been deleted
    is the same thing the other way round, and so is a place that was left out of the earlier
    run -- the repo round without ``--argo-applications`` records fewer fields than the one
    with it.
    """
    shared = sorted(set(previous.fields) & set(current.fields))
    return Fingerprint(fields={name: previous.fields[name] for name in shared}).compare(
        Fingerprint(fields={name: current.fields[name] for name in shared}), replaced=replaced
    )


def replace_record(fingerprint: Fingerprint, path: str | Path, *, replaced: Iterable[str] = ()) -> list[str]:
    """Hold a record that is already there to what it says, and only then overwrite it.

    Overwriting it silently makes the record a tally instead of a check: with the earlier hashes
    gone there is nothing left for a changed plaintext to disagree with, and the round then
    reports the new hash as the truth. Measured on a real file, ``repositories[0].password``
    swapped for a different plaintext and re-encrypted: the round exited 0 and the final check
    said CLEAN, both of them reading the record that same round had just written.

    A deviation therefore stops the round and leaves the earlier record where it is -- that
    record is the evidence. ``replaced`` names the fields that are MEANT to read differently,
    which is the whole of the difference between the key round and the PAT round.

    Returns the objections, empty when there are none -- and then the record is written.
    """
    recorded = Path(path)
    if recorded.is_file():
        previous = Fingerprint.load(recorded)
        objections = content_drift(previous, fingerprint, replaced)
        if objections:
            print(f"\nFAIL the fingerprint recorded in {path} disagrees with what is there now:")
            for objection in objections:
                print(f"  {objection}")
            print("Left as it was: it is from the earlier round and it is the evidence.")
            return objections
        for name in sorted(set(previous.fields) - set(fingerprint.fields)):
            print(f"  no longer in the collection: {name}")
        for name in sorted(set(fingerprint.fields) - set(previous.fields)):
            print(f"  new in the collection since the last round: {name}")
    fingerprint.save(path)
    return []


def update_record(path: str | Path, updates: dict[str, str]) -> list[str]:
    """Write new hashes for the named fields into an existing record, leaving the rest alone.

    The PAT round replaces three plaintexts that sit in THIS repo's record, the one the key
    round wrote. Without this the very next ``rotate-sops-key.py --verify`` reports "content
    changed" on all three with nothing wrong -- the same trap the projects record had before the
    two rounds started sharing it, where the verify objected to every password it had itself
    asked to be replaced.

    Only the named fields move. A name the record does not hold is refused rather than added:
    that would mean the round wrote in a place the record does not cover, which is a coverage
    finding and not a bookkeeping detail. No record at all is not an error -- a PAT round may
    run in a tree where no key round has recorded anything.

    Returns the objections, empty when there are none.
    """
    recorded = Path(path)
    if not recorded.is_file():
        return []
    fingerprint = Fingerprint.load(recorded)
    unknown = sorted(name for name in updates if name not in fingerprint.fields)
    if unknown:
        return [f"not in {path}, so this round wrote outside what it records: {name}" for name in unknown]
    for name, digest in updates.items():
        fingerprint.set(name, digest)
    fingerprint.save(recorded)
    return []


# place 1: SOPS files


def sops_files(tree: str | Path) -> list[Path]:
    """Every file with a ``sops:`` metadata block, whatever it is called.

    Matching on the name would miss half of them: next to the ``*.sops.yaml`` files the tree
    holds two ``operations-manager-env-secrets.yaml`` without that suffix.

    ``.claude/worktrees`` is skipped along with ``.git``, and that is not cosmetic. A worktree
    checked out INSIDE the repo is a second copy of these same files, so without this the walk
    finds them again per worktree: measured on this checkout, 21 became 118 over four of them.
    A rotation would then rewrite secrets in somebody else's working copy, leave uncommitted
    changes behind there, and count those copies into the fingerprint it compares against.
    """
    found: list[Path] = []
    for path in sorted(Path(tree).rglob("*.yaml")):
        if ".git" in path.parts or WORKTREE_DIR in path.parts:
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

    Uses ``opi.utils.sops.decrypt_sops_with_key``: exactly this function, with an explicit
    key instead of the one from the environment, already lives there. A second copy
    alongside it quietly grows different behaviour.
    """
    return decrypt_sops_with_key(str(path), private_key)


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


# places 2 and 3: loose encrypted values, outside a SOPS file and outside a project file


@dataclass(frozen=True)
class LooseValue:
    """One encrypted value in a plain text file, with what is needed to write it back.

    ``line_number`` is the line the value sits on; ``None`` means the file IS the value -- a
    whole armored AGE block and nothing else, the shape ``projects/age-secret-github.txt``
    has. Those are the only two shapes that can be written back from text without risk. An
    armored block EMBEDDED in a larger file carries its own indentation (a YAML block
    scalar), and replacing that span would re-emit it flush left; that shape belongs to the
    SOPS round or to ``rotate_project_file``, and the coverage guard says so by name.
    """

    path: Path
    key: str
    value: str
    line_number: int | None

    @property
    def name(self) -> str:
        return self.key


def _de_indent(block: str) -> str:
    """The armored block with every line's leading whitespace removed.

    Base64 tolerates whitespace, but ``age`` does not accept an indented armor. This is a
    READ path only -- an indented block is never written back through here.
    """
    return "\n".join(line.strip() for line in block.splitlines())


def loose_values(path: str | Path) -> list[LooseValue]:
    """Every encrypted value in a text file that can be converted line by line.

    Works on text and not on YAML, the two ``configmap.yaml`` included: there the env
    content sits in a literal block scalar, and a YAML round trip would re-emit that whole
    block.

    Two shapes come out. A ``base64+age:`` value is base64 and therefore always a single
    line, so a line-scoped replacement touches exactly that one field -- wherever on the line
    it sits, which is what brings a Python literal like ``opi/core/config.py``'s
    ``PROJECT_REPO_PASSWORD`` in alongside an env line. A file that is nothing but an armored
    block is the value itself and is rewritten whole.

    A name that repeats within one file gets its line number appended: the fingerprint is a
    dict keyed on ``path#name``, and two fields under one key would drop one of them silently.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    found: list[LooseValue] = []
    used: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _LOOSE_VALUE.finditer(line):
            before = _NAME_BEFORE.search(line[: match.start()])
            key = before.group(0) if before else f"line{number}"
            if key in used:
                key = f"{key}:{number}"
            used.add(key)
            found.append(LooseValue(path=path, key=key, value=match.group("value"), line_number=number))
    if not found and is_age_encrypted(text.strip()):
        found.append(LooseValue(path=path, key="<file>", value=text.strip(), line_number=None))
    return found


def write_loose_value(field_: LooseValue, new_value: str) -> None:
    """Replace exactly one value, leaving everything around it intact.

    Written through a temporary file in the same directory and moved into place with
    ``os.replace``, for the same reason the YAML writer does it: a torn ``configmap.yaml``,
    ``config.py`` or ``.env`` is worse than an unconverted one, and it would be discovered by a
    deployment rather than by this tool.
    """
    path = field_.path
    if field_.line_number is None:
        text = path.read_text(encoding="utf-8")
        if field_.value not in text:
            raise ConversionFailed(f"{path} no longer holds the expected value")
        content = text.replace(field_.value, new_value)
    else:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        index = field_.line_number - 1
        if field_.value not in lines[index]:
            raise ConversionFailed(f"{path}:{field_.line_number} no longer holds the expected value")
        lines[index] = lines[index].replace(field_.value, new_value)
        content = "".join(lines)

    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# the coverage guard: what carries ciphertext, and does anything reach it


#: The files outside the SOPS tree that carry a loose encrypted value. They are easily
#: forgotten, because ``sops rotate`` does not see them, and this list is the one part of the
#: worklist that cannot select on the recipient: outside a SOPS file the text does not say
#: which key it belongs to. So the list is checked instead -- see ``coverage_gaps()``.
#:
#: ``.env`` is on it deliberately: that file really is loaded during local development, so
#: without conversion that stops working the moment A goes away.
#:
#: The last two read like examples and are not. ``PROJECT_REPO_PASSWORD`` is the default
#: ``odcn-production`` falls back to (``sandboxed-local`` overrides it), and the migration script
#: carries a copy of that same value. Both open with the platform key.
#:
#: ``projects/age-secret-github.txt`` stood here too, a whole file that was one armored block.
#: It was removed from the tree: nothing read it and the token in it was withdrawn.
#:
#: It lives here and not in ``sops_rotation`` because BOTH rounds walk it. The key round
#: re-encrypts every value on the list; the PAT round replaces the ones that hold the shared
#: GitHub token, and skips the git-server passwords next to them -- see ``is_github_token``.
LOOSE_VALUE_FILES = (
    "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml",
    "operations-manager/python/.env",
    "operations-manager/python/scripts/migrate_project_to_production.py",
)


def loose_fingerprint_key(field_: LooseValue) -> str:
    """The name one loose value goes under in the fingerprint."""
    return f"{field_.path}#{field_.name}"


def loose_paths() -> list[Path]:
    """The files of ``LOOSE_VALUE_FILES`` that exist in this working tree."""
    return [REPO / name for name in LOOSE_VALUE_FILES if (REPO / name).is_file()]


def all_loose_values(paths: list[Path]) -> list[LooseValue]:
    """Every loose encrypted value in the named files, as they stand NOW."""
    return [field_ for path in paths for field_ in loose_values(path)]


def is_github_token(plaintext: str) -> bool:
    """Whether a decrypted value is a GitHub token, by the shapes the secret scanner knows.

    This is what tells the PAT carriers from the other loose values next to them. The
    ``odcn-production`` configmap and ``.env`` hold ``GIT_PROJECTS_SERVER_PASSWORD`` and
    ``GIT_ARGO_APPLICATIONS_PASSWORD``, which are the platform's OWN git-server credentials on
    the same platform key; writing a GitHub PAT over those would take OPI's access to its three
    repositories away. Measured on the plaintext rather than settled by a list of file names,
    because a list is what has to be kept right by hand and the shapes are already written down
    once, in ``secret_scan.RULES``.
    """
    return any(pattern.search(plaintext) for pattern in GITHUB_TOKEN_RULES)


def is_real_ciphertext(value: str) -> bool:
    """Whether this really is AGE ciphertext, decided without any key.

    The tree holds values with the right SHAPE and no content: ``base64+age:AAAA`` in a test,
    a shortened block in a feature doc. Measured over this tree, 34 files carry real
    ciphertext against 43 with only the shape, so skipping this check would put nine entries
    nobody can act on next to the six real exceptions -- and that is how a guard goes stale,
    the same reason the secret scanner runs an AGE candidate past ``age-keygen`` instead of
    alarming on the prefix.

    So the armor is unwrapped and the AGE header is read: a real file opens with
    ``age-encryption.org/v1`` and carries a recipient stanza and a MAC line.
    """
    form = form_of(value)
    if form is None:
        return False
    try:
        armored = to_age_block(value, form)
    except (ValueError, UnicodeDecodeError):
        return False
    match = _ARMOR.search(armored)
    if not match:
        return False
    try:
        raw = base64.b64decode("".join(match.group("body").split()))
    except ValueError:
        return False
    return raw.startswith(b"age-encryption.org/v1\n") and b"\n-> " in raw and b"\n--- " in raw


def encrypted_candidates(text: str) -> list[str]:
    """Every value in a text that has the shape of AGE ciphertext, real or not.

    Both storage forms, and the armored blocks come out de-indented so an embedded one (a
    YAML block scalar) can still be read.
    """
    found = [match.group("value") for match in _LOOSE_VALUE.finditer(text)]
    found.extend(_de_indent(match.group(0)) for match in _ARMOR.finditer(text))
    return found


def files_with_ciphertext(tree: str | Path) -> dict[Path, int]:
    """Every tracked file holding real AGE ciphertext, and how many values sit in it.

    This is the inventory ``coverage_gaps()`` hangs off.

    Tracked files and not the working tree: the untracked ``security/`` holds the real keys on
    purpose, and a scratch file is not what a rotation has to reach.
    """
    found: dict[Path, int] = {}
    for path in tracked_files(Path(tree)):
        if skip_reason(path) is not None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count = sum(1 for candidate in encrypted_candidates(text) if is_real_ciphertext(candidate))
        if count:
            found[path] = count
    return found


# place 4: project files


def project_files(directory: str | Path) -> list[Path]:
    """Every project file under the directory, the ones in a subdirectory included.

    ``rglob`` and not ``glob``, and that is the whole point of the function: the projects repo
    keeps an earlier round under ``projects/local-old/``, 8 files carrying 16 platform fields
    that a flat selection leaves on the old key.

    ``.git`` is skipped so that pointing the tool at a clone root instead of at its
    ``projects/`` walks the work tree and not the object store, and ``.claude`` for the reason
    on ``WORKTREE_DIR``.
    """
    return sorted(
        path for path in Path(directory).rglob("*.yaml") if ".git" not in path.parts and WORKTREE_DIR not in path.parts
    )


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


def project_plain_passwords(data: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``repositories[].password`` values holding no ciphertext at all, as (name, plaintext).

    The counterpart of ``project_fields()``, and it exists for the TOKEN half alone.
    ``project_fields()`` selects on ``form_of()``, which is the right filter for the key
    question -- a value with nothing encrypted in it has nothing to re-encrypt, and the round
    rightly walks past it. But that same filter makes a password stored as ``plain:<token>``
    invisible to the check underneath, and ``project_coverage_gaps()`` does not see it either,
    because plain text is not ciphertext. The verdict this feeds is absolute -- nothing
    decrypts to the current PAT any more -- and a token lying there in the clear is that token
    just as much as an encrypted one is.

    ``parse_password_with_prefix`` and not a ``plain:`` strip of our own: it is the function
    OPI reads these values with, so ``age:``, ``base64+age:`` and the bare auto-detected form
    are separated from the plain one exactly once. Only the plain answer comes out here; the
    other three are ciphertext and belong to ``project_fields()``.
    """
    found: list[tuple[str, str]] = []
    for index, repository in enumerate(data.get("repositories") or []):
        password = repository.get("password") if isinstance(repository, dict) else None
        if not isinstance(password, str) or not password or form_of(password) is not None:
            continue
        form, plaintext = parse_password_with_prefix(password)
        if form == "plain" and plaintext:
            found.append((PROJECT_FIELD_REPO_PASSWORD.format(index=index), plaintext))
    return found


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
    means: 52 of the 53 measured project files do not satisfy the CURRENT schema (2.8)
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
    #: Fingerprint keys of the fields whose PLAINTEXT was replaced. Only those may differ
    #: between the two fingerprints, so the list has to name exactly them: a password that was
    #: merely re-encrypted and landed here would be EXCUSED from the content check, and one
    #: that was replaced and did not would fail it.
    replaced: list[str] = field(default_factory=list)
    #: Passwords that were re-encrypted but NOT replaced, because their plaintext is not the
    #: current PAT. One line per field, with the path and the reason.
    kept: list[str] = field(default_factory=list)
    #: Fields that open with neither key, named for the round's own tally.
    unreadable: list[str] = field(default_factory=list)


def nothing_to_do(total: int, on_new_key: int, passwords: int, *, new_pat: str | None, kept: int = 0) -> str:
    """Why this file needs no work, in the terms of the round that is actually running.

    "Already converted" used to cover both rounds; in the PAT round it reads as reassurance
    while the file may still hold the old token. ``broken()`` judges the two sets separately.

    A file whose password is not the current PAT gets its own wording, for the same reason:
    "already holds this PAT" would be a false statement about a value the round deliberately
    did not touch, and the operator has to be able to tell those two apart by reading.
    """
    if new_pat is None:
        return f"already converted ({on_new_key} of {total} fields sit on the new key)"
    if not passwords:
        return "no repository password to replace"
    if kept:
        return f"the repository password is not the current PAT, so it was left as it is ({kept} of {passwords})"
    return f"the repository password already holds this PAT ({on_new_key} of {total} fields sit on the new key)"


async def rotate_project_file(
    path: str | Path,
    old_private_key: str,
    new_private_key: str,
    *,
    new_public_key: str | None = None,
    current_pat: str | None = None,
    new_pat: str | None = None,
    only_private_key: bool = False,
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
    only re-encrypted. ``only_private_key`` narrows the worklist to that one field, which is
    what the KEK round needs: there the repository password stays on the platform key, so
    converting it too would move a field that is supposed to stay where it is.

    **The replacement is conditional, and that is not a nicety.** A password is replaced only
    when its plaintext IS ``current_pat``; anything else is only RE-ENCRYPTED, keeps its
    plaintext, and is named in ``kept`` with its path and the reason. The module docstring has
    the measurement that decided this. Hence ``current_pat`` is required as soon as ``new_pat``
    is given: a PAT round with nothing to compare against is the unconditional round under
    another name.

    The worklist cannot be settled by the key alone: the documented order runs the key round
    first, so by then every password already sits on B while still holding the OLD token, and
    a gate on "does this still open with A?" makes that second round a silent no-op.
    """
    path = Path(path)
    round_report = ProjectRound(path=path)
    new_public_key = new_public_key or public_key_of(new_private_key)
    if new_pat is not None and current_pat is None:
        raise ConversionFailed("a PAT round needs the current PAT: without it every value is replaced blindly")

    data = load_yaml_from_path(str(path))
    if not isinstance(data, dict):
        round_report.skipped = "not a readable project file"
        return round_report

    fields = project_fields(data)
    if only_private_key:
        fields = [(name, value) for name, value in fields if name == PROJECT_FIELD_PRIVATE_KEY]
    # A password stored plain is no ciphertext, so ``project_fields()`` passes it by; in the PAT
    # round it is a carrier like any other, and it leaves this file encrypted.
    plain_passwords = [] if new_pat is None or only_private_key else project_plain_passwords(data)
    if not fields and not plain_passwords:
        round_report.skipped = "no encrypted platform fields in this file"
        return round_report

    # The same key on both sides is a round over the token alone: nothing moves to another key,
    # so a field is only rewritten where its token is replaced.
    key_moves = public_key_of(old_private_key) != new_public_key
    todo: list[tuple[str, str, str, str | None]] = []
    on_new_key = 0
    passwords = 0
    for field_name, value in fields:
        is_password = field_name != PROJECT_FIELD_PRIVATE_KEY
        if is_password:
            passwords += 1
        # Decrypt before deciding, both here and in the argo round: the decision is about the
        # VALUE, and a field that opens with the old key needs its plaintext just as much as
        # one that already sits on the new key.
        plaintext = await decrypt_field(value, old_private_key)
        on_old_key = plaintext is not None
        if plaintext is None:
            plaintext = await decrypt_field(value, new_private_key)
            if plaintext is None:
                round_report.unreadable.append(f"{path}#{field_name}")
                round_report.skipped = f"{field_name} opens with neither key"
                return round_report
            on_new_key += 1

        replacement: str | None = None
        if new_pat is not None and is_password:
            if plaintext == new_pat:
                pass  # already the new token; only the key half can still be outstanding
            elif plaintext == current_pat:
                replacement = new_pat
            else:
                round_report.kept.append(
                    f"{path}#{field_name}: the plaintext is not the current PAT, so it was left as it is"
                )
        if replacement is not None or (on_old_key and key_moves):
            todo.append((field_name, value, old_private_key if on_old_key else new_private_key, replacement))

    plain_todo: list[tuple[str, str, str]] = []
    for field_name, plaintext in plain_passwords:
        passwords += 1
        if new_pat is not None and plaintext in (current_pat, new_pat):
            plain_todo.append((field_name, plaintext, new_pat))
        else:
            round_report.kept.append(
                f"{path}#{field_name}: the plaintext is not the current PAT, so it was left as it is"
            )

    if not todo and not plain_todo:
        round_report.skipped = nothing_to_do(
            len(fields), on_new_key, passwords, new_pat=new_pat, kept=len(round_report.kept)
        )
        return round_report

    round_report.validation_was_already_red = await validate_project_data(data)

    for field_name, value, opening_key, replacement in todo:
        try:
            conversion = await convert_value(value, opening_key, new_public_key, new_plaintext=replacement)
        except Exception as e:  # age and base64 each raise their own type
            round_report.skipped = f"{field_name} does not open: {e}"
            return round_report
        key = f"{path}#{field_name}"
        round_report.fingerprint_before.set(key, conversion.sha256_before)
        round_report.fingerprint_after.set(key, conversion.sha256_after)
        if conversion.replaced:
            round_report.replaced.append(key)
        round_report.fields.append(field_name)
        set_project_field(data, field_name, conversion.new_value)

    for field_name, plaintext, replacement in plain_todo:
        key = f"{path}#{field_name}"
        round_report.fingerprint_before.set(key, sha256_of(plaintext))
        round_report.fingerprint_after.set(key, sha256_of(replacement))
        if plaintext != replacement:
            round_report.replaced.append(key)
        round_report.fields.append(field_name)
        set_project_field(data, field_name, ensure_base64_age_sync(f"plain:{replacement}", None, new_public_key))

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
    """The result of ``--assert-old-key-dead`` over all five places, plus the coverage list."""

    still_opens_with_old: list[str] = field(default_factory=list)
    does_not_open_with_new: list[str] = field(default_factory=list)
    outside_coverage: list[str] = field(default_factory=list)
    #: Fields whose plaintext is a GitHub token that is not the one ``--pat-new-file`` names. The
    #: key half of this check cannot see these: a value can sit on the new key perfectly and
    #: still hold the withdrawn token, and then the round reports CLEAN while every project
    #: created after it gets a dead credential.
    holds_another_token: list[str] = field(default_factory=list)
    #: Fields whose plaintext IS the current PAT: the "the old PAT is dead" half. A different
    #: question from the one above, and ``check_token`` says why.
    still_holds_current_pat: list[str] = field(default_factory=list)
    #: ArgoCD repository secrets whose password no longer equals the project file they were
    #: derived from, and the coupling findings next to them.
    argo_drift: list[str] = field(default_factory=list)
    #: True when a token was supplied to check against, so the verdict can say which halves it
    #: covers instead of reading as a full CLEAN over a question it never asked.
    token_checked: bool = False
    #: The same, for the current PAT: without it the verdict must not claim the old token is gone.
    current_pat_checked: bool = False
    counted: int = 0
    expected: int | None = None

    @property
    def clean(self) -> bool:
        return (
            not self.still_opens_with_old
            and not self.does_not_open_with_new
            and not self.outside_coverage
            and not self.holds_another_token
            and not self.still_holds_current_pat
            and not self.argo_drift
            and self.count_matches
        )

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
        # A gap is not "a field is wrong" but "a field was never looked at", and that is the
        # worse of the two: without it the verdict below reads CLEAN over an incomplete walk.
        out.extend(f"FAIL carries ciphertext and nothing converts it: {name}" for name in self.outside_coverage)
        out.extend(f"FAIL holds a GitHub token that is not the new one: {name}" for name in self.holds_another_token)
        out.extend(f"FAIL still decrypts to the current PAT: {name}" for name in self.still_holds_current_pat)
        out.extend(f"FAIL {name}" for name in self.argo_drift)
        if self.clean:
            verdict = "CLEAN the old key opens nothing, the new key opens everything"
            if self.token_checked:
                verdict += ", and every GitHub token is the new one"
            if self.current_pat_checked:
                verdict += ", and nothing decrypts to the current PAT any more"
            out.append(verdict)
        return out


def check_sops_file(path: Path, old_private_key: str, new_private_key: str, check: FinalCheck) -> None:
    """One SOPS file: A must fail, B must succeed."""
    check.counted += 1
    name = str(path)
    if sops_plaintext(path, old_private_key) is not None:
        check.still_opens_with_old.append(name)
    if sops_plaintext(path, new_private_key) is None:
        check.does_not_open_with_new.append(name)


async def check_value(
    name: str,
    value: str,
    old_private_key: str,
    new_private_key: str,
    check: FinalCheck,
    pat: str | None = None,
    current_pat: str | None = None,
) -> None:
    """One loose value: A must fail, B must succeed -- and with a token, the CONTENT is judged too.

    The key half and the token half are different questions about the same field, and the second
    one is the reason these arguments exist: a value re-encrypted for the new key still holds
    whatever plaintext it held, so a PAT round that skipped this field leaves a withdrawn token
    behind while every key check says CLEAN. With ``pat`` only a plaintext that IS a GitHub token
    is judged, by ``is_github_token``; the git-server passwords sitting on the same key are not
    the PAT and must not be held to it. ``current_pat`` asks the sharper question underneath,
    which needs no shape at all -- see ``check_token``.
    """
    check.counted += 1
    if await opens_with(value, old_private_key):
        check.still_opens_with_old.append(name)
    plaintext = await decrypt_field(value, new_private_key)
    if plaintext is None:
        check.does_not_open_with_new.append(name)
        return
    if pat is not None or current_pat is not None:
        check_token(name, plaintext, pat, check, current_pat)


def check_token(name: str, plaintext: str, pat: str | None, check: FinalCheck, current_pat: str | None = None) -> None:
    """Hold one decrypted value to the token it has to carry, when it carries one at all.

    Shared by the three places the PAT round writes, so "the old token is nowhere" is one rule
    measured three times rather than three spellings of it.

    Two rules, and they do not overlap. ``pat`` says a GitHub token that is not the new one is
    a finding -- which depends on the token being RECOGNISED as one, so a token shape the
    scanner rules do not carry slips past it. ``current_pat`` is plain equality with the value
    the round was supposed to replace, and needs no shape: that is the real "the old PAT is
    dead" measurement, and the one the round's own conditional replacement can be held to.
    """
    if current_pat is not None and plaintext == current_pat:
        check.still_holds_current_pat.append(name)
    if pat is not None and is_github_token(plaintext) and plaintext != pat:
        check.holds_another_token.append(name)


if __name__ == "__main__":
    print(__doc__)
    print("This is the engine, not a tool. The entry points sit next to it:")
    print("  rotate-sops-key.py, rotate-project-keys.py, replace-git-pat.py, set-sops-key-secret.py")
    raise SystemExit(1)
