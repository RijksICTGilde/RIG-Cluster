"""The round over the project files: one pass, one commit per project, one fingerprint.

Two entry points share this module, because there is exactly one difference between the two
jobs:

    read field -> decrypt with A -> [keep value OR replace it] -> encrypt for B -> write
                                             ^              ^
                                          recrypt       new PAT

``rotate-project-keys.py`` calls this without a PAT, ``replace-git-pat.py`` with one. That is
why each project file is touched ONCE instead of twice: fewer commits, fewer chances to drop
something.

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

**Why validation warns rather than blocks.** Measured on the 45 test project files: 44 fail the
CURRENT schema (2.8) because they are older snapshots that OPI migrates when it reads them.
Refusing on that would block the whole rotation over drift that has nothing to do with the key.
So a file that was already invalid stays convertible with a warning; a file that becomes
invalid BECAUSE of the conversion is skipped. ``find_plaintext_secret_violations`` stays hard
either way -- that is the failure where a rotation leaves a secret in plain form in git.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import (  # type: ignore[reportMissingImports]
    Fingerprint,
    ProjectRound,
    public_key_of,
    rotate_project_file,
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
    can be reverted on its own. No push: the cutover verifies before anything leaves the
    machine.
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
            result.replaced_fields.extend(key for key in report.fingerprint_before.fields if key.endswith(".password"))
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


def broken(result: RoundResult) -> list[ProjectRound]:
    """The skips that are a real problem, as opposed to "nothing left to do here".

    A file that is already converted, or that carries no platform field at all, is not a
    finding. A file whose value opens with neither key is, and the round's exit code has to say
    so -- otherwise a silent skip reads as success.
    """
    harmless = ("already converted", "no encrypted platform fields")
    return [
        round_report
        for round_report in result.skipped
        if round_report.skipped is not None and not round_report.skipped.startswith(harmless)
    ]
