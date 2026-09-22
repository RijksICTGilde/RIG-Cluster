"""Replace the platform AGE key in this repo: the SOPS files and the loose values.

This is the entry point for the first three places the platform key occurs in THIS repo. The
fourth, the project files, lives in ``rotate-project-keys.py``; those sit in a different repo
and belong to a round of their own, with its own commits.

    scripts/rotate-sops-key.py --dry-run             # says what it would do, changes nothing
    scripts/rotate-sops-key.py                       # same questions, runs after confirmation
    scripts/rotate-sops-key.py --verify              # check the fingerprint, months later too
    scripts/rotate-sops-key.py --assert-old-key-dead # the final check over all four places

**Dry run is the default in the sense that matters:** without ``--ja`` not a byte is written
before you have answered yes to "run this?". ``--dry-run`` does not even ask.

**No key on the command line.** The tool asks for the PATHS of the keys, with a default on
every question, and reads them from ``security/`` -- that directory is untracked. A key as an
argument lands in the shell history, in the process table and in every log that records the
command.

**What the fingerprint is.** Per encrypted field the sha256 of the PLAINTEXT, recorded before
the conversion and measured again afterwards. The ciphertext changes on every conversion and
therefore says nothing; the plaintext does not change and says everything. The file never
holds a secret -- only a path, a field name and a hash. It defaults to
``security/fingerprint.json``, so outside version control.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The engine sits next to this file. pyright cannot follow a sys.path that is built at
# runtime, hence the ignore; the import itself is exercised by the tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import (  # type: ignore[reportMissingImports]
    EnvField,
    FinalCheck,
    Fingerprint,
    MissingKey,
    ask_for_path,
    check_sops_file,
    check_value,
    convert_value,
    decrypt_field,
    env_fields,
    load_yaml_from_path,
    opens_with,
    project_fields,
    public_key_of,
    read_key,
    sha256_of,
    sops_files_for,
    sops_plaintext,
    sops_rotate,
    write_env_value,
)

REPO = Path(__file__).resolve().parents[1]

#: The files outside the SOPS tree that carry a ``base64+age:`` value in an env line. They
#: are easily forgotten, because ``sops rotate`` does not see them. ``.env`` is on the list
#: deliberately: that file really is loaded during local development, so without conversion
#: that stops working the moment A goes away.
ENV_FILES = (
    "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml",
    "bootstrap/rig-system/kustomize/operations-manager/overlays/local/configmap.yaml",
    "operations-manager/python/.env",
)

#: The fixed location of the keys, as a full path. Relative would be "security/key.txt", and
#: that is only correct when you happen to invoke from the repo root -- while the ordinary
#: working directory for this project is operations-manager/python.
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
DEFAULT_FINGERPRINT = REPO / "security" / "fingerprint.json"
YES_WORDS = {"ja", "j", "yes", "y"}

# A failed decryption is the EXPECTED outcome here: the final check specifically demands that
# the old key opens nothing, and the plan tells "already converted" from "broken" by trying.
# ``opi.utils.age`` logs such an attempt at ERROR, which in this context produces a stream of
# alarming lines on a successful rotation. The verdict is in this script's output, not in that
# log. ``opi.utils.sops`` is silenced for the same reason: it reports a sops decrypt that did
# not fit the key, which is what the final check is asking for.
for _noisy in ("opi.utils.age", "opi.utils.sops"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)


def short(path: Path) -> str:
    """The path relative to the repo when it is inside it, and the full path otherwise.

    ``relative_to`` raises on a path outside the tree, which would turn a display detail into a
    crash halfway through a rotation -- the worst possible moment for one.
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def sops_fingerprint_key(path: Path) -> str:
    """The name a whole SOPS file goes under in the fingerprint."""
    return f"{path}#<sops-document>"


def env_fingerprint_key(field_: EnvField) -> str:
    """The name one env line goes under in the fingerprint."""
    return f"{field_.path}#{field_.name}"


def env_paths() -> list[Path]:
    return [REPO / name for name in ENV_FILES if (REPO / name).is_file()]


def all_env_fields(paths: list[Path]) -> list[EnvField]:
    """Every ``base64+age:`` env line in the named files, as they stand NOW."""
    return [field_ for path in paths for field_ in env_fields(path)]


def sops_on_either_recipient(old_public: str, new_public: str) -> list[Path]:
    """The SOPS files sitting on either recipient, each once.

    The fingerprint has to cover the same set on both sides of the conversion, and a file
    moves from one recipient to the other while it is being rotated.
    """
    seen: dict[Path, None] = {}
    for public in (old_public, new_public):
        for path in sops_files_for(REPO, public):
            seen.setdefault(path, None)
    return list(seen)


async def fingerprint_now(
    sops_paths: list[Path], fields: list[EnvField], *private_keys: str
) -> tuple[Fingerprint, list[str]]:
    """Measure the plaintext of every named field, with the first key that fits.

    Passing more than one key is what makes the before/after comparison honest. If the
    measurement before the conversion used only A and the one after only B, the SET of fields
    would differ as soon as an earlier round had already converted a few, and "field appeared"
    would read as damage when nothing is wrong.

    Second return value: the fields that opened with none of the keys. Those are not a
    fingerprint but a finding, and the caller decides whether that was the intent.
    """
    fingerprint = Fingerprint()
    closed: list[str] = []
    for path in sops_paths:
        for key in private_keys:
            plain = sops_plaintext(path, key)
            if plain is not None:
                fingerprint.set(sops_fingerprint_key(path), sha256_of(plain))
                break
        else:
            closed.append(sops_fingerprint_key(path))
    for field_ in fields:
        for key in private_keys:
            plain = await decrypt_field(field_.value, key)
            if plain is not None:
                fingerprint.set(env_fingerprint_key(field_), sha256_of(plain))
                break
        else:
            closed.append(env_fingerprint_key(field_))
    return fingerprint, closed


def ask_for_keys(arguments: argparse.Namespace) -> tuple[str, str]:
    """Ask for the two key files and hand back the private halves."""
    reader = (lambda _question: "") if arguments.ja else input
    old = ask_for_path("old key", arguments.old_key, reader=reader)
    new = ask_for_path("new key", arguments.new_key, reader=reader)
    return read_key(old), read_key(new)


@dataclass
class RotationPlan:
    """What is left to do, and what turned out to be done. Both belong on screen."""

    sops: list[Path] = field(default_factory=list)
    env: list[EnvField] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sops) + len(self.env)


async def build_plan(paths: list[Path], old_private: str, new_private: str, old_public: str) -> RotationPlan:
    """Decide per field whether it still needs converting, is done, or opens with neither key.

    That distinction is measured rather than assumed, and it is what makes a second round
    genuinely idempotent. The SOPS side takes care of itself: it selects on recipient, so a
    converted file falls outside the selection. A loose ``base64+age:`` value does not carry
    its recipient in the text, so there a decryption attempt is the only answer -- without it a
    second round would call the already-converted values "to do" and then strand on the
    fingerprint.
    """
    plan = RotationPlan(sops=sops_files_for(REPO, old_public))
    for path in paths:
        for field_ in env_fields(path):
            if await opens_with(field_.value, old_private):
                plan.env.append(field_)
            elif await opens_with(field_.value, new_private):
                plan.already.append(env_fingerprint_key(field_))
            else:
                plan.closed.append(env_fingerprint_key(field_))
    return plan


def show_plan(plan: RotationPlan) -> None:
    """Name what would happen, and what is no longer needed."""
    print(f"\n{len(plan.sops)} SOPS files on the old recipient:")
    for path in plan.sops:
        print(f"  sops rotate  {short(path)}")
    print(f"\n{len(plan.env)} loose base64+age: values on the old key:")
    for field_ in plan.env:
        print(f"  recrypt      {short(field_.path)}:{field_.line_number}  {field_.name}")
    for name in plan.already:
        print(f"  already      {name}")
    for name in plan.closed:
        print(f"  FAIL opens with neither key: {name}")
    print(f"\nTo do: {len(plan.sops)} files + {len(plan.env)} loose values = {plan.total} fields")


async def run_rotation(plan: RotationPlan, old_private: str, old_public: str, new_public: str) -> None:
    for path in plan.sops:
        sops_rotate(path, old_public, new_public, old_private)
        print(f"  converted  {short(path)}")
    for field_ in plan.env:
        conversion = await convert_value(field_.value, old_private, new_public)
        write_env_value(field_.path, field_.line_number, field_.value, conversion.new_value)
        print(f"  converted  {short(field_.path)}:{field_.line_number} {field_.name}")


async def run_final_check(
    old_private: str,
    new_private: str,
    old_public: str,
    new_public: str,
    projects: Path | None,
    expected: int | None,
) -> FinalCheck:
    """A must fail everywhere, B must succeed everywhere, and the count must match.

    Walks the SOPS files on BOTH recipients. Walking only the new one would make a skipped
    file invisible: that one still sits on the old recipient and would fall outside the
    selection.
    """
    check = FinalCheck(expected=expected)
    for path in sops_on_either_recipient(old_public, new_public):
        check_sops_file(path, old_private, new_private, check)
    for path in env_paths():
        for field_ in env_fields(path):
            await check_value(env_fingerprint_key(field_), field_.value, old_private, new_private, check)
    if projects is not None:
        for path in sorted(projects.glob("*.yaml")):
            data = load_yaml_from_path(str(path))
            if not isinstance(data, dict):
                continue
            for field_name, value in project_fields(data):
                await check_value(f"{path}#{field_name}", value, old_private, new_private, check)
    return check


def expected_count(paths: list[Path]) -> int | None:
    """The total number of fields across the fingerprints that exist, or None when there is none.

    The final check walks four places, and those were converted by two tools with a
    fingerprint each. The count that has to match is therefore the SUM; passing only one of
    them compares a part against a whole and always yields a deviation.
    """
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return None
    return sum(len(Fingerprint.load(path).fields) for path in existing)


def rename_keys(old: Path, new: Path, *, yes: bool) -> None:
    """Put the keys under their fixed names, so no manual step is left over.

    ``security/key.txt`` always means "this is the key". Measured: 84 references to that path
    in the Taskfile, in CLAUDE.md and in the installation documentation. If the new key were
    to get a different name, all of those would have to move with it. So the old one shifts to
    ``old_key.txt`` and the new one takes over the fixed name.
    """
    if old.resolve() == CANONICAL_OLD.resolve() and new.resolve() == CANONICAL_NEW.resolve():
        print("Keys already sit under their fixed names.")
        return
    print(f"\nRenaming: {old} -> {CANONICAL_OLD} and {new} -> {CANONICAL_NEW}")
    if not yes and input("Do it? [no]: ").strip().lower() not in YES_WORDS:
        print("Not renamed. Do this by hand, or 84 references keep pointing at the old key.")
        return
    if old.resolve() != CANONICAL_OLD.resolve():
        old.replace(CANONICAL_OLD)
    if new.resolve() != CANONICAL_NEW.resolve():
        new.replace(CANONICAL_NEW)
    print("Renamed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="say what would happen and stop")
    parser.add_argument("--ja", action="store_true", help="take every default and skip the confirmation")
    parser.add_argument("--verify", action="store_true", help="check the fingerprint without converting anything")
    parser.add_argument(
        "--assert-old-key-dead", action="store_true", help="the final check: A opens nothing, B opens everything"
    )
    parser.add_argument(
        "--remove-old-key",
        action="store_true",
        help="delete the old key file, but only when the final check is clean",
    )
    parser.add_argument("--rename", action="store_true", help="put the keys under their fixed names and stop")
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument("--fingerprint", default=str(DEFAULT_FINGERPRINT))
    parser.add_argument(
        "--projects-fingerprint",
        help="the fingerprint rotate-project-keys.py wrote, so the final check's count adds up",
    )
    parser.add_argument(
        "--projects",
        help="directory with project files, so the final check can walk the fourth place too",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    try:
        old_private, new_private = ask_for_keys(arguments)
    except MissingKey as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2

    old_public = public_key_of(old_private)
    new_public = public_key_of(new_private)
    if old_public == new_public:
        print("FAIL the old and the new key are the same key", file=sys.stderr)
        return 2

    projects = Path(arguments.projects) if arguments.projects else None
    fingerprint_path = Path(arguments.fingerprint)
    paths = env_paths()

    if arguments.rename:
        rename_keys(Path(arguments.old_key), Path(arguments.new_key), yes=arguments.ja)
        return 0

    if arguments.assert_old_key_dead or arguments.remove_old_key:
        if projects is None:
            print("NOTE without --projects the final check does not walk the fourth place.")
        fingerprints = [fingerprint_path]
        if arguments.projects_fingerprint:
            fingerprints.append(Path(arguments.projects_fingerprint))
        expected = expected_count(fingerprints) if projects is not None else None
        check = await run_final_check(old_private, new_private, old_public, new_public, projects, expected)
        for line in check.lines():
            print(line)
        if not check.clean:
            return 1
        if arguments.remove_old_key:
            if projects is None:
                print("REFUSED the old key does not go away without --projects: the fourth", file=sys.stderr)
                print("place was not walked, so a project may still sit on the old key.", file=sys.stderr)
                return 1
            old_path = Path(arguments.old_key)
            old_path.unlink(missing_ok=True)
            print(f"Old key removed: {old_path}")
        return 0

    if arguments.verify:
        if not fingerprint_path.is_file():
            print(f"FAIL no fingerprint to check against: {fingerprint_path}", file=sys.stderr)
            return 2
        wanted = Fingerprint.load(fingerprint_path)
        measured, closed = await fingerprint_now(
            sops_on_either_recipient(old_public, new_public), all_env_fields(paths), new_private
        )
        objections = wanted.compare(measured)
        for name in closed:
            print(f"FAIL does not open with the new key: {name}")
        for objection in objections:
            print(f"FAIL {objection}")
        if objections or closed:
            return 1
        print(f"CLEAN {len(measured.fields)} fields readable with the new key and unchanged in content")
        return 0

    plan = await build_plan(paths, old_private, new_private, old_public)
    show_plan(plan)
    if plan.closed:
        print("\nSTOPPED there are fields that open with neither key.", file=sys.stderr)
        return 1
    if plan.total == 0:
        print("\nNothing to do: no field sits on the old key any more.")
        return 0

    if arguments.dry_run:
        print("\nDry run: nothing was changed.")
        return 0

    if not arguments.ja and input("\nRun this? [no]: ").strip().lower() not in YES_WORDS:
        print("Nothing changed.")
        return 0

    print("\nRecording the fingerprint of the plaintext...")
    fingerprint_before, closed = await fingerprint_now(
        sops_on_either_recipient(old_public, new_public), all_env_fields(paths), old_private, new_private
    )
    if closed:
        for name in closed:
            print(f"FAIL opens with neither key: {name}", file=sys.stderr)
        return 1
    fingerprint_before.save(fingerprint_path)
    print(f"  {len(fingerprint_before.fields)} fields -> {fingerprint_path}")

    print("\nConverting...")
    await run_rotation(plan, old_private, old_public, new_public)

    print("\nChecking with the new key...")
    fingerprint_after, closed = await fingerprint_now(
        sops_files_for(REPO, new_public), all_env_fields(paths), new_private
    )
    objections = fingerprint_before.compare(fingerprint_after)
    for name in closed:
        print(f"FAIL does not open with the new key: {name}", file=sys.stderr)
    for objection in objections:
        print(f"FAIL {objection}", file=sys.stderr)
    if closed or objections:
        return 1
    print(f"  {len(fingerprint_after.fields)} fields readable with the new key and unchanged in content")

    print("\nDone. Still to do:")
    print("  1. scripts/rotate-project-keys.py on a fresh clone of the projects repo")
    print("  2. commit and push, only once step 1 has been verified")
    print("  3. scripts/set-sops-key-secret.py, and restart the operations-manager")
    print("  4. scripts/rotate-sops-key.py --assert-old-key-dead --projects <clone>/projects")
    return 0
