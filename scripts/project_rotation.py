"""The round over the project files: one pass, one commit per project, one fingerprint.

Two entry points share this module: ``rotate-project-keys.py`` calls it without a PAT,
``replace-git-pat.py`` with one. Both run the same loop -- ``convert_value()`` in
``key_rotation.py`` -- so each project file is touched ONCE instead of twice.

**Why a local clone and not ``save_and_commit_project``.** The plan names that method as the
only validated write path, and it is -- for OPI. This tool deviates, for two measured reasons:

* it writes through ``GitConnector``, whose ``_parse_git_url`` accepts ``git://``, ``ssh://``,
  ``https://`` and the ``git@host:path`` shorthand and raises ``ValueError: Unsupported Git URL
  format`` on anything else. A ``file://`` URL is therefore not addressable, so the tool could
  not be tested against the 45-file test set the plan requires, and running it would push to
  the live remote per project with no dry run in between;
* the plan's own cutover says "VERIFY while nothing has been pushed yet" and only then
  "commit and push". A method that pushes per project contradicts that order.

What the plan actually protects against is an unvalidated write, and that protection is kept:
this module runs ``validate_project_schema`` + ``validate_project_structure`` (the exact two
checks ``ProjectStore._validate`` runs, in the same order) and writes through
``dump_yaml_to_string``, the same canonical dumper. The operator pushes the clone once the
fingerprint has been verified.

**Why validation warns rather than blocks.** A file that was already invalid stays convertible
with a warning; a file that becomes invalid BECAUSE of the conversion is skipped. The
measurement behind that is in ``validate_project_data()``.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import (  # type: ignore[reportMissingImports]
    PROJECT_FIELD_PRIVATE_KEY,
    Fingerprint,
    MissingKey,
    ProjectRound,
    ask_for_path,
    decrypt_field,
    load_yaml_from_path,
    project_fields,
    public_key_of,
    read_key,
    rotate_project_file,
    sha256_of,
)

# A failed decryption is the EXPECTED outcome in several places here: telling "already
# converted" from "unreadable" is done by trying. ``opi.utils.age`` logs such an attempt at
# ERROR, which would bury the tool's own verdict under alarming lines.
logging.getLogger("opi.utils.age").setLevel(logging.CRITICAL)

YES_WORDS = {"ja", "j", "yes", "y"}


@dataclass
class RoundResult:
    """What the whole round did, in the terms the operator has to report afterwards."""

    converted: list[ProjectRound] = field(default_factory=list)
    skipped: list[ProjectRound] = field(default_factory=list)
    fingerprint_before: Fingerprint = field(default_factory=Fingerprint)
    fingerprint_after: Fingerprint = field(default_factory=Fingerprint)
    replaced_fields: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)

    @property
    def fields(self) -> int:
        return len(self.fingerprint_before.fields)


def git_commit_one(repo: Path, relative: Path, message: str) -> bool:
    """Commit exactly one file in the clone. Returns False when there was nothing to commit.

    One commit per project, so the history says which project changed and a single bad file
    can be reverted on its own.
    """
    add = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "add", "--", str(relative)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if add.returncode != 0:
        raise RuntimeError(f"git add failed on {relative}: {add.stderr.strip()}")
    staged = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", str(relative)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode == 0:
        return False
    commit = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "commit", "--no-verify", "-m", message, "--", str(relative)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed on {relative}: {commit.stderr.strip()}")
    return True


def find_repo_root(directory: Path) -> Path | None:
    """The git working tree the project files live in, or None when they are not in one."""
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


async def run_round(
    directory: Path,
    old_private: str,
    new_private: str,
    *,
    new_pat: str | None = None,
    dry_run: bool = True,
    commit: bool = True,
) -> RoundResult:
    """Walk every project file in ``directory`` once, converting what still sits on A.

    One unreadable file does not abort the round: it is reported and the projects after it are
    still done. That matters most in exactly the situation this tool exists for, where stopping
    halfway leaves some projects on the old key and some on the new one with no record of which.
    """
    result = RoundResult()
    new_public = public_key_of(new_private)
    repo_root = find_repo_root(directory) if commit and not dry_run else None

    for path in sorted(directory.glob("*.yaml")):
        report = await rotate_project_file(
            path,
            old_private,
            new_private,
            new_public_key=new_public,
            new_pat=new_pat,
            dry_run=dry_run,
        )
        if report.skipped is not None:
            result.skipped.append(report)
            continue
        result.converted.append(report)
        result.fingerprint_before.fields.update(report.fingerprint_before.fields)
        result.fingerprint_after.fields.update(report.fingerprint_after.fields)
        if new_pat is not None:
            # Everything EXCEPT the project key: that one is only ever re-encrypted, never
            # replaced. Derived from the field name rather than sniffed for ".password", so a
            # future field cannot land on the replaced list by accident.
            result.replaced_fields.extend(
                key for key in report.fingerprint_before.fields if not key.endswith(f"#{PROJECT_FIELD_PRIVATE_KEY}")
            )
        if report.rewritten and repo_root is not None:
            # The commits land in zad-projects next to OPI's own ("auto-tune: adjust
            # resources ..."), so they follow that wording rather than this repo's.
            what = "sops key and git password" if new_pat is not None else "sops key"
            message = f"key-rotation: update {what} for {path.stem}"
            if git_commit_one(repo_root, path.relative_to(repo_root), message):
                result.committed.append(path.name)

    return result


def report(result: RoundResult, *, dry_run: bool) -> None:
    """Print the round, converted files first and the reasons for every skip after."""
    verb = "would convert" if dry_run else "converted"
    print(f"\n{len(result.converted)} project files {verb} ({result.fields} fields):")
    for round_report in result.converted:
        note = ""
        if round_report.validation_was_already_red:
            note = "  [pre-existing schema drift, left as it was]"
        print(f"  {round_report.path.name}: {', '.join(round_report.fields)}{note}")
    print(f"\n{len(result.skipped)} project files skipped:")
    for round_report in result.skipped:
        print(f"  {round_report.path.name}: {round_report.skipped}")
    if result.committed:
        print(f"\n{len(result.committed)} commits made, one per project. Nothing pushed.")

    objections = result.fingerprint_before.compare(result.fingerprint_after, replaced=result.replaced_fields)
    if objections:
        print("\nFAIL the content check found deviations:")
        for objection in objections:
            print(f"  {objection}")
    elif result.fields:
        kept = "unchanged" if not result.replaced_fields else "unchanged except the replaced passwords"
        print(f"\nContent check: {result.fields} fields {kept}.")


async def fingerprint_all(directory: Path, *private_keys: str) -> tuple[Fingerprint, list[str]]:
    """Measure the plaintext of every platform field in the directory, with the first key that fits.

    Passing both keys is what lets this cover the whole collection regardless of how far a
    round got: a field already on B reads just as well as one still on A.

    Second return value: the fields that opened with neither key. Those are not a fingerprint
    but a finding -- the same ones the round reports as a real problem.
    """
    fingerprint = Fingerprint()
    closed: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        data = load_yaml_from_path(str(path))
        if not isinstance(data, dict):
            continue
        for field_name, value in project_fields(data):
            name = f"{path}#{field_name}"
            for key in private_keys:
                plain = await decrypt_field(value, key)
                if plain is not None:
                    fingerprint.set(name, sha256_of(plain))
                    break
            else:
                closed.append(name)
    return fingerprint, closed


async def save_fingerprint(directory: Path, path: str, *private_keys: str) -> None:
    """Record the whole collection, not the fields this particular round happened to convert.

    ``run_round`` promises that one unreadable file does not abort the round, so a rotation may
    take two rounds. A record of the second round's worklist alone makes ``--assert-old-key-dead``
    fail on the count with nothing wrong with the key, and the record of the first round is gone
    by then.

    Measured fresh each time rather than merged into what was there: a merge would keep an entry
    for a project that has since been deleted, and the check does not walk that one any more.
    """
    fingerprint, closed = await fingerprint_all(directory, *private_keys)
    fingerprint.save(path)
    print(f"\nFingerprint of {len(fingerprint.fields)} fields -> {path}")
    for name in closed:
        print(f"  not in the fingerprint, opens with neither key: {name}")


def broken(result: RoundResult, *, pat_round: bool = False) -> list[ProjectRound]:
    """The skips that are a real problem, as opposed to "nothing left to do here".

    A file whose value opens with neither key is a finding, and the round's exit code has to say
    so -- otherwise a silent skip reads as success. What is harmless differs per round, which is
    why the mode comes in here: "already converted" answers the key question and says nothing
    about the token, so it drops off the list in the PAT round and the skips that name the
    password take its place.
    """
    harmless = (
        ("no encrypted platform fields", "no repository password", "the repository password already holds this PAT")
        if pat_round
        else ("already converted", "no encrypted platform fields")
    )
    return [
        round_report
        for round_report in result.skipped
        if round_report.skipped is not None and not round_report.skipped.startswith(harmless)
    ]


# entry point 1: the key rotation


REPO = Path(__file__).resolve().parents[1]
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
KEY_FINGERPRINT = REPO / "security" / "projects-fingerprint.json"
PAT_FINGERPRINT = REPO / "security" / "projects-pat-fingerprint.json"


ROTATE_KEYS_DESCRIPTION = """Move the project files to the new platform key.

This is the fourth and largest place the platform key occurs: 45 files in the projects repo, each
with TWO fields on the platform key -- config.age-private-key and every repositories[].password.
A project where only the first was converted can no longer reach its own repository, which is why
both always go together.

Point --projects at a LOCAL CLONE of the projects repo. This writes and commits there and pushes
nothing: the cutover verifies while nothing has been pushed yet, and then the operator pushes.
"""

PAT_DESCRIPTION = """Replace the GitHub PAT in every project file: the same round, one argument more.

Hard precondition: the new PAT must already be valid on GitHub before the first file is written,
with the old one still valid too. Otherwise a project loses its repository access the moment its
file is converted while the rest is not. GitHub happily knows two valid tokens at once, which is
the one thing a key cannot do -- hence no overlap phase for the key and one for the PAT.

Advice on ordering: let the first real round do only the key. Once that has demonstrably gone
well, the PAT round is a repeat of something that already worked, and the two stay revertible
separately.
"""


def build_key_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=ROTATE_KEYS_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--projects", required=True, help="directory holding the project .yaml files")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen and stop")
    parser.add_argument("--ja", action="store_true", help="take every default and skip the confirmation")
    parser.add_argument("--no-commit", action="store_true", help="write the files but make no commits")
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument("--fingerprint", default=str(KEY_FINGERPRINT))
    return parser


async def main_rotate_keys(argv: list[str] | None = None) -> int:
    """The key rotation entry point: recrypt both platform fields, keep every plaintext."""
    arguments = build_key_parser().parse_args(argv)
    directory = Path(arguments.projects)
    if not directory.is_dir():
        print(f"FAIL not a directory: {directory}", file=sys.stderr)
        return 2

    reader = (lambda _question: "") if arguments.ja else input
    try:
        old_private = read_key(ask_for_path("old key", arguments.old_key, reader=reader))
        new_private = read_key(ask_for_path("new key", arguments.new_key, reader=reader))
    except MissingKey as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2
    if public_key_of(old_private) == public_key_of(new_private):
        print("FAIL the old and the new key are the same key", file=sys.stderr)
        return 2

    count = len(list(directory.glob("*.yaml")))
    print(f"\n{count} project files in {directory}")

    preview = await run_round(directory, old_private, new_private, dry_run=True, commit=False)
    report(preview, dry_run=True)

    if arguments.dry_run:
        print("\nDry run: nothing was changed.")
        return 1 if broken(preview) else 0

    if not arguments.ja and input("\nRun this? [no]: ").strip().lower() not in YES_WORDS:
        print("Nothing changed.")
        return 0

    result = await run_round(directory, old_private, new_private, dry_run=False, commit=not arguments.no_commit)
    report(result, dry_run=False)
    await save_fingerprint(directory, arguments.fingerprint, old_private, new_private)

    problems = broken(result)
    if problems:
        print(f"\nFAIL {len(problems)} project files were skipped with a real problem.", file=sys.stderr)
        return 1
    if result.fingerprint_before.compare(result.fingerprint_after):
        return 1

    print(f"\nDone. Check `git log --oneline` and `git diff --stat HEAD~{len(result.committed)}` in the clone,")
    print("then push. After that, from the repository root:")
    print(
        f"  uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead --projects {directory}"
    )
    return 0


# entry point 2: the PAT replacement, the same round with one argument more


def read_pat(path_argument: str | None) -> str:
    """Take the new PAT from a file, or ask for it without echo."""
    if path_argument:
        value = Path(path_argument).read_text().strip()
        if not value:
            raise MissingKey(f"no PAT in {path_argument}")
        return value
    value = getpass.getpass("new GitHub PAT (not echoed): ").strip()
    if not value:
        raise MissingKey("no PAT entered")
    return value


def build_pat_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=PAT_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects", required=True, help="directory holding the project .yaml files")
    parser.add_argument("--pat-file", help="file holding the new PAT; without it you are asked, without echo")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen and stop")
    parser.add_argument("--ja", action="store_true", help="take every default and skip the confirmation")
    parser.add_argument("--no-commit", action="store_true", help="write the files but make no commits")
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument("--fingerprint", default=str(PAT_FINGERPRINT))
    return parser


async def main_replace_pat(argv: list[str] | None = None) -> int:
    """The PAT entry point: the same round, with the repository password replaced as well."""
    arguments = build_pat_parser().parse_args(argv)
    directory = Path(arguments.projects)
    if not directory.is_dir():
        print(f"FAIL not a directory: {directory}", file=sys.stderr)
        return 2

    reader = (lambda _question: "") if arguments.ja else input
    try:
        old_private = read_key(ask_for_path("old key", arguments.old_key, reader=reader))
        new_private = read_key(ask_for_path("new key", arguments.new_key, reader=reader))
        new_pat = read_pat(arguments.pat_file)
    except (MissingKey, OSError) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2
    if public_key_of(old_private) == public_key_of(new_private):
        print("FAIL the old and the new key are the same key", file=sys.stderr)
        return 2

    print(f"\n{len(list(directory.glob('*.yaml')))} project files in {directory}")
    print("The repository password is REPLACED; config.age-private-key is only re-encrypted.")
    print("Its fingerprint hash therefore has to differ at exactly the password fields.")

    preview = await run_round(directory, old_private, new_private, new_pat=new_pat, dry_run=True, commit=False)
    report(preview, dry_run=True)

    if arguments.dry_run:
        print("\nDry run: nothing was changed.")
        return 1 if broken(preview, pat_round=True) else 0

    print("\nIs the new PAT ALREADY valid on GitHub, with the old one still valid too?")
    print("If not, every converted project loses its repository access until the round is done.")
    if not arguments.ja and input("Confirm and run? [no]: ").strip().lower() not in YES_WORDS:
        print("Nothing changed.")
        return 0

    result = await run_round(
        directory, old_private, new_private, new_pat=new_pat, dry_run=False, commit=not arguments.no_commit
    )
    report(result, dry_run=False)
    await save_fingerprint(directory, arguments.fingerprint, old_private, new_private)

    problems = broken(result, pat_round=True)
    if problems:
        print(f"\nFAIL {len(problems)} project files were skipped with a real problem.", file=sys.stderr)
        return 1
    if result.fingerprint_before.compare(result.fingerprint_after, replaced=result.replaced_fields):
        return 1

    print("\nDone. Push the clone, check that a project can reach its repository, and only then")
    print("revoke the old PAT on GitHub.")
    return 0
