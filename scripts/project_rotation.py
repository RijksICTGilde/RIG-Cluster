"""The round over the project files: one pass, one commit per project, one fingerprint.

Two entry points share this module: ``rotate-project-keys.py`` calls it without a PAT,
``replace-git-pat.py`` with one. Both run the same loop -- ``convert_value()`` in
``key_rotation.py`` -- so each project file is touched ONCE instead of twice.

**Why a local clone and not ``save_and_commit_project``.** The plan names that method as the
only validated write path, and it is -- for OPI. This tool deviates, for two measured reasons:

* it writes through ``GitConnector``, whose ``_parse_git_url`` accepts ``git://``, ``ssh://``,
  ``https://`` and the ``git@host:path`` shorthand and raises ``ValueError: Unsupported Git URL
  format`` on anything else. A ``file://`` URL is therefore not addressable, so the tool could
  not be tested against the projects test set the plan requires, and running it would push to
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
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))

from argo_rotation import (  # type: ignore[reportMissingImports]
    ArgoPlan,
    plan_argo_round,
    write_repository_secret,
)
from key_rotation import (  # type: ignore[reportMissingImports]
    Fingerprint,
    LooseValue,
    MissingKey,
    ProjectRound,
    all_loose_values,
    ask_for_path,
    convert_value,
    decrypt_field,
    files_with_ciphertext,
    is_github_token,
    load_yaml_from_path,
    loose_fingerprint_key,
    loose_paths,
    project_fields,
    project_files,
    public_key_of,
    read_key,
    replace_record,
    rotate_project_file,
    sha256_of,
    update_record,
    write_loose_value,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

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
    #: Passwords re-encrypted but not replaced, one line each, path and reason included.
    kept: list[str] = field(default_factory=list)
    #: Fields that open with neither key. Named, because a count of "converted" says nothing
    #: about them and they are the third of the three numbers the dry run has to keep apart.
    unreadable: list[str] = field(default_factory=list)
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


def display(path: Path, directory: Path) -> str:
    """The file as the operator has to recognise it: with its subdirectory, when it has one.

    The bare name would do while the selection was flat. It no longer is, and two project files
    with the same name in different subdirectories would print as one line twice.
    """
    try:
        return str(path.relative_to(directory))
    except ValueError:
        return str(path)


async def run_round(
    directory: Path,
    old_private: str,
    new_private: str,
    *,
    current_pat: str | None = None,
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

    for path in project_files(directory):
        report = await rotate_project_file(
            path,
            old_private,
            new_private,
            new_public_key=new_public,
            current_pat=current_pat,
            new_pat=new_pat,
            dry_run=dry_run,
        )
        # Both lists are filled on a skipped file too: a password left alone is a finding
        # whether or not the file had something else to convert, and so is an unreadable field.
        result.kept.extend(report.kept)
        result.unreadable.extend(report.unreadable)
        if report.skipped is not None:
            result.skipped.append(report)
            continue
        result.converted.append(report)
        result.fingerprint_before.fields.update(report.fingerprint_before.fields)
        result.fingerprint_after.fields.update(report.fingerprint_after.fields)
        # Exactly the fields whose PLAINTEXT was replaced, as the conversion itself reported
        # them; ``ProjectRound.replaced`` says what that list excuses.
        result.replaced_fields.extend(report.replaced)
        if report.rewritten and repo_root is not None:
            # The commits land in zad-projects next to OPI's own ("auto-tune: adjust
            # resources ..."), so they follow that wording rather than this repo's.
            what = "sops key and git password" if new_pat is not None else "sops key"
            message = f"key-rotation: update {what} for {path.stem}"
            if git_commit_one(repo_root, path.relative_to(repo_root), message):
                result.committed.append(display(path, directory))

    return result


def report(result: RoundResult, directory: Path, *, dry_run: bool) -> None:
    """Print the round, converted files first and the reasons for every skip after."""
    verb = "would convert" if dry_run else "converted"
    print(f"\n{len(result.converted)} project files {verb} ({result.fields} fields):")
    for round_report in result.converted:
        note = ""
        if round_report.validation_was_already_red:
            note = "  [pre-existing schema drift, left as it was]"
        print(f"  {display(round_report.path, directory)}: {', '.join(round_report.fields)}{note}")
    print(f"\n{len(result.skipped)} project files skipped:")
    for round_report in result.skipped:
        print(f"  {display(round_report.path, directory)}: {round_report.skipped}")
    if result.kept:
        # Named and not just counted: these are the fields that need a decision made by hand.
        print(f"\n{len(result.kept)} repository passwords re-encrypted but NOT replaced:")
        for line in result.kept:
            print(f"  {line}")
    if result.unreadable:
        print(f"\n{len(result.unreadable)} fields open with neither key:")
        for line in result.unreadable:
            print(f"  FAIL {line}")
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
    for path in project_files(directory):
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


async def save_fingerprint(directory: Path, path: str, *private_keys: str, replaced: Iterable[str] = ()) -> list[str]:
    """Record the whole collection, and hold a record that is already there to what it says.

    ``run_round`` promises that one unreadable file does not abort the round, so a rotation may
    take two rounds. A record of the second round's worklist alone makes ``--assert-old-key-dead``
    fail on the count with nothing wrong with the key, and the record of the first round is gone
    by then. Hence the whole collection, measured fresh each time rather than merged into what
    was there: a merge would keep an entry for a project that has since been deleted, and the
    check does not walk that one any more.

    Measuring fresh is also why the record is replaced through ``replace_record()``, which
    compares before it overwrites. Why that guard exists, and why the repo round shares it
    rather than carrying a copy, is in ``replace_record`` itself.

    Returns the objections, empty when there are none.
    """
    fingerprint, closed = await fingerprint_all(directory, *private_keys)
    objections = replace_record(fingerprint, path, replaced=replaced)
    if objections:
        return objections
    print(f"\nFingerprint of {len(fingerprint.fields)} fields -> {path}")
    for name in closed:
        print(f"  not in the fingerprint, opens with neither key: {name}")
    return []


def broken(result: RoundResult, *, pat_round: bool = False) -> list[ProjectRound]:
    """The skips that are a real problem, as opposed to "nothing left to do here".

    A file whose value opens with neither key is a finding, and the round's exit code has to say
    so -- otherwise a silent skip reads as success. What is harmless differs per round, which is
    why the mode comes in here: "already converted" answers the key question and says nothing
    about the token, so it drops off the list in the PAT round and the skips that name the
    password take its place.

    A password left alone because it is not the current PAT belongs on the harmless side: the
    exit code says whether the round could do its work, and this file it could. After the key
    round every field already sits on the new key, so such a project has nothing left to
    convert and lands among the skips -- an exit 1 would then report failure on exactly the
    fields the round correctly declined to touch. They are named in the report instead. The
    argo half treats its drift the same way, for the same reason.
    """
    harmless = (
        (
            "no encrypted platform fields",
            "no repository password",
            "the repository password already holds this PAT",
            "the repository password is not the current PAT",
        )
        if pat_round
        else ("already converted", "no encrypted platform fields")
    )
    return [
        round_report
        for round_report in result.skipped
        if round_report.skipped is not None and not round_report.skipped.startswith(harmless)
    ]


def coverage_gaps(directory: Path) -> list[Path]:
    """Tracked files under the directory carrying real ciphertext that this round does not walk.

    The round's own numbers cannot answer this. The worklist and the fingerprint come out of the
    same selection, so whatever that selection misses is missing from both and the two agree with
    each other -- which is how a flat glob converted 90 fields, recorded 90 and left 16 in a
    subdirectory opening with the old key, under a final check that said CLEAN. This asks the tree
    what it holds instead, so a ``.yml``, a file without an extension or a directory nobody thought
    of stops the round rather than passing through it unseen.
    """
    covered = set(project_files(directory))
    return sorted(path for path in files_with_ciphertext(directory) if path not in covered)


def worklist(directory: Path) -> tuple[list[Path], int]:
    """The files this round walks, and the exit code that says not to start. 0 means go.

    An existing directory holding no project file at all used to be a silent exit 0 over "0
    project files", and that reads as a finished round to whoever runs the step after it -- a
    wrong clone, an empty one. That refusal mirrors the one ``--argo-applications`` already
    gets. A tracked file carrying ciphertext the selection does not reach is the other half; it
    stops the round for the reason ``coverage_gaps()`` gives, with the exit code the SOPS round
    uses for the same finding.
    """
    found = project_files(directory)
    if not found:
        print(f"FAIL no project files under {directory}", file=sys.stderr)
        print("Nothing there matches *.yaml, so this round would convert nothing and exit 0.", file=sys.stderr)
        print("Check the path: the documented one is <clone>/projects.", file=sys.stderr)
        return [], 2
    gaps = coverage_gaps(directory)
    if gaps:
        # Opens like the SOPS round's stop on the same finding, so an operator who has seen one
        # recognises the other.
        print("STOPPED a tracked file carries ciphertext this round does not walk:", file=sys.stderr)
        for path in gaps:
            print(f"  {display(path, directory)}", file=sys.stderr)
        print("Converting anyway ends in a count that matches over an incomplete walk.", file=sys.stderr)
        return [], 1
    return found, 0


# the PAT round's second place: the loose values in THIS repo that carry the shared token


@dataclass
class LooseField:
    """One loose encrypted value, with its plaintext and the key that opened it."""

    field_: LooseValue
    plaintext: str
    opening_key: str
    #: Whether it was the OLD key that opened it, so this value still has to move to B even
    #: when its plaintext is left alone.
    on_old_key: bool = False

    @property
    def name(self) -> str:
        return f"{self.field_.path}#{self.field_.name}"


@dataclass
class LooseRound:
    """The verdict over the loose values: which carry the token, which do not, which are broken."""

    carriers: list[LooseField] = field(default_factory=list)
    others: list[LooseField] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    converted: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    #: Carriers left alone because their plaintext is not the current PAT, with the reason.
    kept: list[str] = field(default_factory=list)


async def classify_loose_values(paths: list[Path], old_private: str, new_private: str) -> LooseRound:
    """Split the loose values into the ones holding a GitHub token and the ones that do not.

    Both keys go in, in that order, for the reason the project round takes both: after the key
    round every one of these sits on the new key while still holding the OLD token, so a gate on
    "does this still open with the old key?" would make the PAT round a silent no-op here.
    """
    result = LooseRound()
    for field_ in all_loose_values(paths):
        for index, key in enumerate((old_private, new_private)):
            plaintext = await decrypt_field(field_.value, key)
            if plaintext is not None:
                # By position and not by comparing the key strings: the two are refused when
                # they are equal, but "which one opened it" must not hang on that refusal.
                entry = LooseField(field_=field_, plaintext=plaintext, opening_key=key, on_old_key=index == 0)
                (result.carriers if is_github_token(plaintext) else result.others).append(entry)
                break
        else:
            result.closed.append(f"{field_.path}#{field_.name}")
    return result


async def run_loose_round(
    result: LooseRound, new_public: str, current_pat: str, new_pat: str, *, dry_run: bool
) -> dict[str, str]:
    """Put the new token in every loose value that carries the CURRENT one. Returns the new hashes.

    The same conditional the other two places apply, for the same reason: "the plaintext is a
    GitHub token" is a shape, and a shape does not say which token. A value holding an older one
    keeps its plaintext and is named in ``kept`` -- but it is still RE-ENCRYPTED when it sits on
    the old key, because the two halves are separate promises: the round declines to change the
    content, not to finish the key rotation. Leaving it on A would fail the final check on a
    field the round deliberately did not replace.

    The hashes go back into this repo's own record through ``update_record``: this is the one
    round where a plaintext is supposed to change. A kept value is not in there, and must not
    be: its plaintext is the same, so its hash is too.
    """
    updates: dict[str, str] = {}
    for entry in result.carriers:
        if entry.plaintext == new_pat:
            result.already.append(entry.name)
            continue
        if entry.plaintext != current_pat:
            result.kept.append(f"{entry.name}: a GitHub token that is not the current PAT, so it was left as it is")
            if entry.on_old_key:
                conversion = await convert_value(entry.field_.value, entry.opening_key, new_public)
                if not dry_run:
                    write_loose_value(entry.field_, conversion.new_value)
            continue
        conversion = await convert_value(entry.field_.value, entry.opening_key, new_public, new_plaintext=new_pat)
        if not dry_run:
            write_loose_value(entry.field_, conversion.new_value)
        updates[loose_fingerprint_key(entry.field_)] = conversion.sha256_after
        result.converted.append(entry.name)
    return updates


def report_loose(result: LooseRound, *, dry_run: bool) -> None:
    """Print the loose values the way the round judged them, all three groups by name.

    The group that is LEFT ALONE is printed too, and that is the point of the section: those are
    the platform's own git-server passwords, on the same key and in the same files, and a round
    that quietly wrote a GitHub token over them would take OPI's access to its three
    repositories down without saying a word.
    """
    verb = "would replace" if dry_run else "replaced"
    print(f"\n{len(result.converted)} loose values {verb} (they carry the current PAT):")
    for name in result.converted:
        print(f"  {name}")
    for name in result.already:
        print(f"  already holds this token: {name}")
    for name in result.kept:
        print(f"  left as it is: {name}")
    print(f"\n{len(result.others)} loose values left alone (no GitHub token in the plaintext):")
    for entry in result.others:
        print(f"  {entry.name}")
    for name in result.closed:
        print(f"  FAIL opens with neither key: {name}")


# the PAT round's third place: the ArgoCD repository secrets, in a clone of another repo


async def run_argo_round(
    clone: Path,
    directory: Path,
    old_private: str,
    new_private: str,
    *,
    dry_run: bool,
    current_pat: str | None = None,
    new_pat: str | None = None,
) -> tuple[ArgoPlan, list[str]]:
    """Bring every ArgoCD repository secret back in step, and replace the token it holds.

    Without a PAT the value comes from the project file: these secrets are DERIVED, so whatever
    the project round decided about a repository is what lands here. A repository the project
    round did not convert -- its password absent, ``plain:``, or in another form -- keeps its own
    value, exactly as OPI would write it. The whole sandbox is that shape.

    With a PAT the decision is the secret's own password against the current token, the same
    conditional the project round applies; ``plan_argo_round`` says why deriving would undo it.
    That also settles the dry run: the answer no longer depends on whether the project files
    have been written yet, so the preview and the real run measure the same thing.

    Returns the plan and the lines naming what was written.
    """
    plan = await plan_argo_round(clone, directory, old_private, new_private, current_pat=current_pat, new_pat=new_pat)
    converted: list[str] = []
    for secret, repository, password in plan.todo:
        if not dry_run:
            write_repository_secret(secret, password)
        converted.append(f"{display(secret.path, clone)} <- {display(repository.path, directory)}")
    return plan, converted


def report_argo(plan: ArgoPlan, converted: list[str], clone: Path, directory: Path, *, dry_run: bool) -> None:
    """Print the argo round: what moved, what was left, what is not ours, and the coupling."""
    pairing = plan.pairing
    closed = plan.closed
    verb = "would update" if dry_run else "updated"
    print(f"\n{len(converted)} ArgoCD repository secrets {verb} in {clone}:")
    for line in converted:
        print(f"  {line}")
    if not converted and pairing.pairs:
        print(f"  none: all {len(pairing.pairs)} already hold what their project file holds")
    if plan.kept:
        print(f"\n{len(plan.kept)} repository secrets left as they are:")
        for secret, repository, reason in plan.kept:
            print(
                f"  {display(secret.path, clone)} ({secret.name}): {reason}"
                f" -- derived from {display(repository.path, directory)}#{repository.field_name}"
            )
    if plan.drift:
        print(f"\n{len(plan.drift)} repository secrets disagree with their project file:")
        for line in plan.drift:
            print(f"  {line}")
    if pairing.ssh_form:
        print(f"\n{len(pairing.ssh_form)} repository secrets on an SSH key, untouched by a PAT round:")
        for secret in pairing.ssh_form:
            print(f"  {display(secret.path, clone)} ({secret.credential_form})")
    if pairing.without_platform_password:
        print(f"\n{len(pairing.without_platform_password)} secrets whose project file holds no platform-key password:")
        for secret, repository in pairing.without_platform_password:
            print(f"  {display(secret.path, clone)} <- {display(repository.path, directory)}#{repository.field_name}")
        print("  The project round does not convert those either, so there is nothing to derive.")
    if pairing.repositories_without_secret:
        print(f"\n{len(pairing.repositories_without_secret)} project repositories have no secret in this clone:")
        for repository in pairing.repositories_without_secret:
            print(f"  {repository.project}/{repository.repository} ({', '.join(repository.secret_names)})")
        print("  Those are projects OPI has not processed since; it writes these files then.")
        print("  Not a stop: the only repair is to reprocess every project, and that is what a")
        print("  key rotation must not set off.")
    for name in closed:
        print(f"  FAIL opens with neither key: {name}")
    for secret in pairing.secrets_without_project:
        print(f"  FAIL no project file accounts for this secret: {display(secret.path, clone)} ({secret.name})")
    for path in pairing.unreadable:
        print(f"  FAIL opens with neither key: {display(path, clone)}")


def argo_problems(plan: ArgoPlan) -> list[str]:
    """The argo findings that stop the round, as lines.

    A secret no project file accounts for is one: nothing maintains its password, this round
    cannot derive a value for it, and after the old token is withdrawn it hands ArgoCD a dead
    credential. The other direction -- a project repository without a secret -- is reported but
    does not stop; see ``report_argo``. Drift does not stop the round either: the round no
    longer flattens it, and what a differing secret should hold is a decision for a person.
    """
    pairing = plan.pairing
    return (
        [f"no project file accounts for {secret.path} ({secret.name})" for secret in pairing.secrets_without_project]
        + [f"opens with neither key: {path}" for path in pairing.unreadable]
        + [f"opens with neither key: {name}" for name in plan.closed]
    )


# entry point 1: the key rotation


REPO = Path(__file__).resolve().parents[1]
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
#: One record for the collection, written by BOTH rounds. The PAT round had its own file and
#: nothing read it: ``rotate-sops-key.py --verify --projects`` and ``--assert-old-key-dead``
#: both default to this one, so after the PAT round the verify said "content changed" on every
#: password with nothing wrong. Sharing the record costs no check, because ``replaced=`` names
#: the password fields as the ones that are MEANT to differ and the key field next to them
#: still has to be unchanged.
KEY_FINGERPRINT = REPO / "security" / "projects-fingerprint.json"
#: The two tokens, on disk in the same untracked ``security/`` the keys live in and answered
#: the same way. Two and not one: the round has to be told what the current PAT is.
CANONICAL_PAT_CURRENT = REPO / "security" / "pat_current.txt"
CANONICAL_PAT_NEW = REPO / "security" / "pat_new.txt"
#: The record ``rotate-sops-key.py`` keeps for THIS repo. The PAT round does not write it, it
#: CORRECTS the entries of the loose values whose plaintext it replaces -- see ``update_record``.
REPO_FINGERPRINT = REPO / "security" / "fingerprint.json"


ROTATE_KEYS_DESCRIPTION = """Move the project files to the new platform key.

This is the fourth and largest place the platform key occurs: 53 files in the projects repo, 8 of
them in a subdirectory, each with TWO fields on the platform key -- config.age-private-key and
every repositories[].password. A project where only the first was converted can no longer reach
its own repository, which is why both always go together.

Point --projects at the projects/ directory in a LOCAL CLONE of the projects repo, not at the
clone itself. This writes and commits there and pushes nothing: the cutover verifies while
nothing has been pushed yet, and then the operator pushes.
"""

PAT_DESCRIPTION = """Replace the GitHub PAT everywhere it is held: three places, one round.

The PAT round is as wide as the key round, and for the same reason: a value that is left behind
does not fail loudly, it fails at the next sync or at the next project that gets created.

  project files   repositories[].password, per project, the same loop as the key round
  this repo       the loose values whose plaintext IS a GitHub token. Measured per value, not
                  by a list of file names: the configmaps and .env hold the platform's own
                  git-server passwords on that same key, and those must NOT be overwritten
  argo clone      the password inside every ArgoCD repository secret, which argo_manager put
                  there out of the project file. --argo-applications is required: without it
                  ArgoCD keeps talking to the withdrawn token and nothing says so

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
    # Resolved: every fingerprint key is "<path>#<field>", and rotate-sops-key.py --verify
    # compares those names against this record.
    directory = Path(arguments.projects).resolve()
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

    found, refusal = worklist(directory)
    if refusal:
        return refusal
    print(f"\n{len(found)} project files in {directory}")

    preview = await run_round(directory, old_private, new_private, dry_run=True, commit=False)
    report(preview, directory, dry_run=True)

    if arguments.dry_run:
        print("\nDry run: nothing was changed.")
        return 1 if broken(preview) else 0

    if not arguments.ja and input("\nRun this? [no]: ").strip().lower() not in YES_WORDS:
        print("Nothing changed.")
        return 0

    result = await run_round(directory, old_private, new_private, dry_run=False, commit=not arguments.no_commit)
    report(result, directory, dry_run=False)
    # No ``replaced``: this round re-encrypts and changes no plaintext, so every hash that moved
    # is a finding.
    drifted = await save_fingerprint(directory, arguments.fingerprint, old_private, new_private)

    problems = broken(result)
    if problems:
        print(f"\nFAIL {len(problems)} project files were skipped with a real problem.", file=sys.stderr)
        return 1
    if drifted:
        return 1
    if result.fingerprint_before.compare(result.fingerprint_after):
        return 1

    if result.committed:
        print(f"\nDone. Check `git log --oneline` and `git diff --stat HEAD~{len(result.committed)}` in the clone,")
        print("then push. After that, from the repository root:")
    else:
        # A second round of a rotation that went well: everything already sits on the new key.
        # "git diff --stat HEAD~0" would be the working tree against itself and show nothing.
        print("\nDone. Nothing was left to convert, so there is nothing to commit or push. Still:")
    print("  uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead")
    print(f"  --projects {directory} --argo-applications <zad-argo clone>")
    return 0


# entry point 2: the PAT replacement, the same round with the two tokens added


def read_pat(path: str | Path) -> str:
    """Take a PAT out of a file, the same shape the key files have.

    A file and not a prompt, and never an argument, for the reason ``ask_for_path`` gives: the
    two PATs are answered the way ``old_key.txt`` and ``key.txt`` are.
    """
    value = Path(path).read_text().strip()
    if not value:
        raise MissingKey(f"no PAT in {path}")
    return value


def report_pat_totals(project: RoundResult, loose: LooseRound, plan: ArgoPlan, *, dry_run: bool) -> None:
    """The round in three numbers, counted apart over all three places.

    One number cannot carry this. "58 fields converted" is true of a round that replaced 58
    tokens and of one that replaced 56 and left two older ones alone, and the difference between
    those two is the whole question the operator has to answer before revoking anything. So:
    what was replaced, what was kept (and where), and what could not be read at all.
    """
    verb = "would be" if dry_run else "were"
    replaced = len(project.replaced_fields) + len(loose.converted) + len(plan.todo)
    kept = len(project.kept) + len(loose.kept) + len(plan.kept)
    unreadable = len(project.unreadable) + len(loose.closed) + len(plan.closed) + len(plan.pairing.unreadable)
    print("\nThe PAT round in three numbers, over the project files, this repo and the argo clone:")
    print(f"  {replaced} fields {verb} replaced with the new PAT")
    print(f"  {kept} fields {verb} left as they are (their value is not the current PAT)")
    print(f"  {unreadable} fields open with neither key")
    for line in [*project.kept, *loose.kept]:
        print(f"  kept: {line}")
    for secret, repository, reason in plan.kept:
        print(f"  kept: {secret.path} ({secret.name}): {reason} -- from {repository.path}#{repository.field_name}")


def build_pat_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=PAT_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects", required=True, help="directory holding the project .yaml files")
    parser.add_argument(
        "--argo-applications",
        required=True,
        help="clone of zad-argo-user-applications, holding the ArgoCD repository secrets",
    )
    parser.add_argument(
        "--pat-current-file",
        default=str(CANONICAL_PAT_CURRENT),
        help="file holding the PAT that is being replaced; only fields holding THIS value are replaced",
    )
    parser.add_argument(
        "--pat-new-file",
        "--pat-file",
        dest="pat_new_file",
        default=str(CANONICAL_PAT_NEW),
        help="file holding the new PAT (--pat-file is the old name for this)",
    )
    parser.add_argument("--dry-run", action="store_true", help="say what would happen and stop")
    parser.add_argument("--ja", action="store_true", help="take every default and skip the confirmation")
    parser.add_argument("--no-commit", action="store_true", help="write the files but make no commits")
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument("--fingerprint", default=str(KEY_FINGERPRINT))
    parser.add_argument(
        "--repo-fingerprint",
        default=str(REPO_FINGERPRINT),
        help="the record rotate-sops-key.py wrote for THIS repo; the replaced loose values are"
        " written back into it, so the next --verify does not object to its own replacement",
    )
    return parser


async def main_replace_pat(argv: list[str] | None = None) -> int:
    """The PAT entry point: the project files, the loose values in this repo, and the argo clone."""
    arguments = build_pat_parser().parse_args(argv)
    directory = Path(arguments.projects).resolve()
    if not directory.is_dir():
        print(f"FAIL not a directory: {directory}", file=sys.stderr)
        return 2
    argo = Path(arguments.argo_applications).resolve()
    if not argo.is_dir():
        print(f"FAIL not a directory: {argo}", file=sys.stderr)
        return 2

    reader = (lambda _question: "") if arguments.ja else input
    try:
        old_private = read_key(ask_for_path("old key", arguments.old_key, reader=reader))
        new_private = read_key(ask_for_path("new key", arguments.new_key, reader=reader))
        current_pat = read_pat(ask_for_path("current PAT", arguments.pat_current_file, reader=reader))
        new_pat = read_pat(ask_for_path("new PAT", arguments.pat_new_file, reader=reader))
    except (MissingKey, OSError) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2
    if public_key_of(old_private) == public_key_of(new_private):
        print("FAIL the old and the new key are the same key", file=sys.stderr)
        return 2
    if current_pat == new_pat:
        # The same refusal the keys get, and for the same reason: a round with nothing to move
        # to would report every field as "already holds this PAT" and read as a finished round.
        print("FAIL the current and the new PAT are the same token", file=sys.stderr)
        return 2
    new_public = public_key_of(new_private)

    found, refusal = worklist(directory)
    if refusal:
        return refusal
    print(f"\n{len(found)} project files in {directory}")
    print("A repository password is replaced only where it IS the current PAT; anything else is")
    print("re-encrypted with its value untouched and named below. config.age-private-key is")
    print("never replaced, so its fingerprint hash has to stay identical.")

    preview = await run_round(
        directory, old_private, new_private, current_pat=current_pat, new_pat=new_pat, dry_run=True, commit=False
    )
    report(preview, directory, dry_run=True)

    loose_preview = await classify_loose_values(loose_paths(), old_private, new_private)
    await run_loose_round(loose_preview, new_public, current_pat, new_pat, dry_run=True)
    report_loose(loose_preview, dry_run=True)

    argo_preview, argo_converted = await run_argo_round(
        argo, directory, old_private, new_private, dry_run=True, current_pat=current_pat, new_pat=new_pat
    )
    report_argo(argo_preview, argo_converted, argo, directory, dry_run=True)
    report_pat_totals(preview, loose_preview, argo_preview, dry_run=True)
    blocking = argo_problems(argo_preview) + [f"opens with neither key: {n}" for n in loose_preview.closed]

    if arguments.dry_run:
        print("\nDry run: nothing was changed.")
        if blocking:
            print(f"STOPPED {len(blocking)} findings before any file would be written.", file=sys.stderr)
            return 1
        return 1 if broken(preview, pat_round=True) else 0

    if blocking:
        # Before the confirmation and before a single byte: a round that converted the project
        # files and then stopped on the argo clone leaves the two halves on different tokens,
        # which is the state this tool exists to avoid.
        print(f"\nSTOPPED {len(blocking)} findings; nothing was written:", file=sys.stderr)
        for line in blocking:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("\nIs the new PAT ALREADY valid on GitHub, with the old one still valid too?")
    print("If not, every converted project loses its repository access until the round is done.")
    if not arguments.ja and input("Confirm and run? [no]: ").strip().lower() not in YES_WORDS:
        print("Nothing changed.")
        return 0

    result = await run_round(
        directory,
        old_private,
        new_private,
        current_pat=current_pat,
        new_pat=new_pat,
        dry_run=False,
        commit=not arguments.no_commit,
    )
    report(result, directory, dry_run=False)
    drifted = await save_fingerprint(
        directory, arguments.fingerprint, old_private, new_private, replaced=result.replaced_fields
    )

    loose_result = await classify_loose_values(loose_paths(), old_private, new_private)
    updates = await run_loose_round(loose_result, new_public, current_pat, new_pat, dry_run=False)
    report_loose(loose_result, dry_run=False)
    record_objections = update_record(arguments.repo_fingerprint, updates)
    for objection in record_objections:
        print(f"FAIL {objection}", file=sys.stderr)

    # The same question the preview asked: the secret's own password against the current token,
    # which does not depend on the project files having been written first.
    plan, converted = await run_argo_round(
        argo, directory, old_private, new_private, dry_run=False, current_pat=current_pat, new_pat=new_pat
    )
    report_argo(plan, converted, argo, directory, dry_run=False)
    report_pat_totals(result, loose_result, plan, dry_run=False)
    argo_left = argo_problems(plan)
    for line in argo_left:
        print(f"FAIL {line}", file=sys.stderr)

    problems = broken(result, pat_round=True)
    if problems:
        print(f"\nFAIL {len(problems)} project files were skipped with a real problem.", file=sys.stderr)
        return 1
    if drifted or record_objections or argo_left or loose_result.closed:
        return 1
    if result.fingerprint_before.compare(result.fingerprint_after, replaced=result.replaced_fields):
        return 1

    print("\nDone. The projects clone has a commit per project; the argo clone and this repo are")
    print("left in the working tree, the same as the key round does -- commit them there.")
    print("Push all three, check that a project can reach its repository and that ArgoCD still")
    print("renders, and only then revoke the old PAT on GitHub. The final check proves the rest:")
    print("  uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead")
    print(f"  --projects {directory} --argo-applications {argo}")
    print(f"  --pat-current-file {arguments.pat_current_file} --pat-new-file {arguments.pat_new_file}")
    print("The current-PAT half of that check is the one that says the old token is dead.")
    return 0
