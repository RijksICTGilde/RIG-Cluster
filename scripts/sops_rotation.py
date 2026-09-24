"""Replace the platform AGE key in this repo: the SOPS files and the loose values.

This is the entry point for everything in THIS repo: the SOPS files, the loose encrypted values
and the project file in ``projects/``. With ``--argo-applications`` it also takes the ArgoCD
repository secrets in a clone of zad-argo-user-applications, which sit on the platform recipient
too. The project files in the zad-projects repo live in ``rotate-project-keys.py``; those sit in
a different repo and belong to a round of their own, with its own commits.

    uv run --project operations-manager/python python scripts/rotate-sops-key.py --dry-run             # says what it would do, changes nothing
    uv run --project operations-manager/python python scripts/rotate-sops-key.py                       # same questions, runs after confirmation
    uv run --project operations-manager/python python scripts/rotate-sops-key.py --verify              # check the fingerprint, months later too
    uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead # the final check over all five places

Paste those lines whole: ``scripts/README.md`` says why the launcher is part of the command.

The final check answers three questions, and two of them need a flag. Without one it is about
the KEY alone: the old one opens nothing, the new one opens everything. A field can satisfy
that and still hold the withdrawn GitHub token, because re-encrypting changes the key and not
the content. ``--pat-new-file`` adds the question "is every GitHub token the new one", over the
loose values, the project files and the ArgoCD repository secrets at once.

``--pat-current-file`` adds the sharper one underneath: no field, in any of those three places,
may still decrypt to the token that was replaced. That is the real "the old PAT is dead"
assertion, and ``check_token`` in the engine says why it is not the same question as the one
above. ``replace-git-pat.py`` is what writes them, and it takes the same two files.

**Dry run is the default in the sense that matters:** without ``--ja`` not a byte is written
before you have answered yes to "run this?". ``--dry-run`` does not even ask.

**No key on the command line.** The tool asks for the PATHS of the keys, with a default on
every question, and reads them from the untracked ``security/``.

**What the fingerprint is.** Per encrypted field the sha256 of the PLAINTEXT, recorded before
the conversion and measured again afterwards, so you can show that nothing changed but the
key. It holds no secret, only a path, a field name and a hash, and it defaults to
``security/fingerprint.json``, outside version control.
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

# The argo half of the final check lives with the round that writes those files, so the two
# cannot drift apart on what a repository secret is.
from argo_rotation import check_repository_secrets  # type: ignore[reportMissingImports]
from key_rotation import (  # type: ignore[reportMissingImports]
    ConversionFailed,
    FinalCheck,
    Fingerprint,
    KeyExists,
    LooseValue,
    MissingKey,
    ProjectRound,
    all_loose_values,
    ask_for_new_key,
    ask_for_path,
    check_sops_file,
    check_token,
    check_value,
    convert_value,
    decrypt_field,
    encrypted_candidates,
    files_with_ciphertext,
    is_real_ciphertext,
    load_yaml_from_path,
    loose_fingerprint_key,
    loose_paths,
    loose_values,
    opens_with,
    project_fields,
    project_files,
    project_plain_passwords,
    public_key_of,
    read_key,
    replace_record,
    rotate_project_file,
    sha256_of,
    sops_files,
    sops_files_for,
    sops_plaintext,
    sops_rotate,
    write_loose_value,
)

# ``--verify`` checks the record the projects round wrote rather than growing a second walk over
# the same clone, which would then have to be kept in step with that round by hand.
from project_rotation import fingerprint_all, read_pat  # type: ignore[reportMissingImports]

REPO = Path(__file__).resolve().parents[1]

#: Tracked files that hold real AGE ciphertext this rotation deliberately does NOT convert,
#: with the reason. Anything carrying ciphertext that is neither converted nor named here is a
#: coverage gap and stops the tool. Every entry is a hand-written claim, and
#: ``check_exceptions()`` holds it to that claim with the old key.
COVERAGE_EXCEPTIONS = {
    "docs/doorloop-rc108/projectbestand.yaml": "a walkthrough's copy of a sandbox project file",
    "docs/doorloop-rc110/projectbestand.yaml": "a walkthrough's copy of a sandbox project file",
    "operations-manager/python/tests/e2e/fixtures/projects/test-project.yaml": "an e2e fixture on a test key",
    "operations-manager/python/tests/e2e/fixtures/projects/test-project-detail.yaml": "an e2e fixture on a test key",
    "operations-manager/python/tests/e2e/fixtures/projects/test-project-with-services.yaml": (
        "an e2e fixture on a test key"
    ),
    "operations-manager/python/tests/e2e/test_lotc_niet_goedgekeurd_domein.py": "an e2e fixture on a test key",
}

#: This repo carries a project file of its own, and it holds a repository password on the platform
#: key -- measured, one field. It is a project file, so ``rotate_project_file`` converts it, but it
#: lives HERE, so this tool owns it: pointing rotate-project-keys.py at a clone of zad-projects
#: would never reach it, and the final check would pass with a field still on the old key.
OWN_PROJECTS = REPO / "projects"

#: Full paths, not relative: the ordinary working directory for this project is
#: operations-manager/python, so "security/key.txt" only resolves from the repo root.
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
DEFAULT_FINGERPRINT = REPO / "security" / "fingerprint.json"

#: Where ``rotate-project-keys.py`` writes ITS fingerprint. A default and not a required flag
#: because the documented step 8 (``--remove-old-key``) is a bare command: without it the count
#: would cover this repo alone and a rotation where nothing is wrong would fail on
#: "count differs".
DEFAULT_PROJECTS_FINGERPRINT = REPO / "security" / "projects-fingerprint.json"
YES_WORDS = {"ja", "j", "yes", "y"}

# A failed decryption is the EXPECTED outcome here: the final check demands that the old key
# opens nothing, and "already converted" is told from "broken" by trying. Both modules log such
# an attempt at ERROR, which would put a stream of alarming lines on a successful rotation. The
# verdict is in this script's output, not in that log.
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


def own_project_paths() -> list[Path]:
    """This repo's own project files, for the CIPHERTEXT questions. Flat on purpose.

    ``projects/ideas/`` holds sketches that are not project files, and this repo's ciphertext is
    already measured against ``coverage_gaps()``, which walks the tree recursively -- so a
    project file appearing in a subdirectory here is a named gap that stops the round, not a
    silent miss. That is what buys the flat selection for the three callers that ask a question
    about ciphertext: ``own_project_fields``, ``covered_in`` and ``build_plan``.

    It buys nothing for the token half. A ``plain:<token>`` is not ciphertext, so no gap list
    sees it and there is no net underneath; ``own_plain_passwords`` walks this same directory
    recursively for that reason.
    """
    return sorted(OWN_PROJECTS.glob("*.yaml")) if OWN_PROJECTS.is_dir() else []


def gaps_in(tree: Path, covered: set[Path]) -> list[Path]:
    """Tracked files under one tree holding real ciphertext that ``covered`` does not name."""
    return sorted(path for path in files_with_ciphertext(tree) if path not in covered)


def covered_in(tree: Path) -> set[Path]:
    """What this rotation reaches inside one tree of ``sops_trees()``.

    This repo holds four kinds of place. A clone of zad-argo-user-applications holds one, its
    SOPS files: ``argo_manager.py`` writes the repository secrets there through
    ``encrypt_to_sops_files_or_fail`` and nothing else. That last word is a claim about another
    module, which is why the sweep below measures it rather than repeating it.
    """
    if tree == REPO:
        return {
            *sops_files(REPO),
            *loose_paths(),
            *own_project_paths(),
            *(REPO / name for name in COVERAGE_EXCEPTIONS),
        }
    return set(sops_files(tree))


def coverage_gaps(trees: list[Path] | None = None) -> list[Path]:
    """Tracked files holding real ciphertext that no place of this rotation reaches.

    The SOPS round selects on the recipient and takes care of itself, but the loose values
    cannot: outside a SOPS file the text does not say which key it belongs to, so there the
    worklist is a list of paths. This is the check on that list, and it is the thing that was
    missing: three committed values sat outside every place the tool walked and
    ``--assert-old-key-dead`` reported CLEAN over them.

    It needs no key, so every dry run runs it.

    Over EVERY tree of ``sops_trees()`` and not this repo alone: the argo clone used to be the
    one tree whose coverage was reasoned about instead of measured, while ``--remove-old-key``
    goes irreversibly ahead on the same CLEAN.

    A SOPS file counts as covered by having SOPS metadata, not by its recipient: a file on
    another key is still a file ``sops rotate`` owns, and the recipient selection decides
    whether it is touched.
    """
    trees = trees if trees is not None else [REPO]
    return sorted(path for tree in trees for path in gaps_in(tree, covered_in(tree)))


def project_coverage_gaps(projects: Path) -> list[Path]:
    """The same check as ``coverage_gaps()``, over the clone the project round walked.

    That round has the problem this repo does not: its worklist and the fingerprint it compares
    against are one and the same walk, so neither half can see what that walk misses. This one
    does not come out of it -- git says which files the tree tracks, and any of them holding real
    ciphertext that the selection does not reach is a gap.

    No exception list of its own. Every tracked file under the directory either is a project
    file the round converts, or it is the finding.
    """
    return gaps_in(projects, set(project_files(projects)))


def sops_trees(argo_applications: Path | None) -> list[Path]:
    """The trees whose SOPS files hang off the platform key.

    This repo, and a clone of ``zad-argo-user-applications`` when one is given. That second one
    is not this repo's business by name but by key: ``argo_manager.py`` writes the ArgoCD
    repository secrets there with ``encrypt_to_sops_files_or_fail(..., SOPS_AGE_PUBLIC_KEY)``,
    so they sit on the PLATFORM recipient, and the sops-plugin next to ArgoCD renders them with
    the very secret step 5 replaces. Left behind, every one of them stops rendering the moment
    the new key is in the cluster.

    Selection stays on the recipient inside each tree, so pointing this at a clone that holds
    files for other keys touches none of them.
    """
    return [REPO, *([argo_applications] if argo_applications is not None else [])]


def sops_on_either_recipient(old_public: str | None, new_public: str, trees: list[Path]) -> list[Path]:
    """The SOPS files sitting on either recipient, each once.

    The fingerprint has to cover the same set on both sides of the conversion, and a file
    moves from one recipient to the other while it is being rotated.

    ``old_public`` may be None, and narrowing to the new recipient alone loses nothing: after a
    completed rotation that is the same set, and a file left behind on the old one shows up
    through the fingerprint as "does not open with the new key".
    """
    seen: dict[Path, None] = {}
    for public in (old_public, new_public):
        if public is None:
            continue
        for tree in trees:
            for path in sops_files_for(tree, public):
                seen.setdefault(path, None)
    return list(seen)


def own_project_fields() -> list[tuple[str, str]]:
    """The platform-keyed fields in this repo's own ``projects/``, as ``(fingerprint key, value)``.

    Same shape as a loose value for the fingerprint's purposes: a name and a ciphertext.
    """
    found: list[tuple[str, str]] = []
    for path in own_project_paths():
        data = load_yaml_from_path(str(path))
        if not isinstance(data, dict):
            continue
        found.extend((f"{path}#{name}", value) for name, value in project_fields(data))
    return found


def own_plain_passwords() -> list[tuple[str, str]]:
    """The plain-text ``repositories[].password`` values in this repo's own ``projects/``.

    Shaped like ``own_project_fields()`` -- ``(fingerprint key, value)`` -- but the value is a
    plaintext and not a ciphertext, because there is nothing encrypted here to open.

    What ``own_project_fields()`` is for the key question, this is for the token one, split the
    way ``project_plain_passwords()`` describes for the clone; here that leaves a
    ``plain:<token>`` invisible to ``coverage_gaps()`` as well. The final check walks this
    repo's ``projects/`` as well as the clone, so without this the same input answers the token
    question on one path and not on the other.

    ``project_files(OWN_PROJECTS)`` and not ``own_project_paths()``, for the reason that
    docstring gives. A withdrawn token lying in the clear one directory down -- and
    ``projects/ideas/`` is five tracked files, one of them carrying a ``repositories:`` list --
    would otherwise get a full CLEAN out of the last gate before ``--remove-old-key``.

    Not counted, for the reason ``run_final_check`` gives at the clone's copy.
    """
    found: list[tuple[str, str]] = []
    for path in project_files(OWN_PROJECTS):
        data = load_yaml_from_path(str(path))
        if not isinstance(data, dict):
            continue
        found.extend((f"{path}#{name}", plaintext) for name, plaintext in project_plain_passwords(data))
    return found


async def fingerprint_now(
    sops_paths: list[Path], fields: list[LooseValue], *private_keys: str
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
                fingerprint.set(loose_fingerprint_key(field_), sha256_of(plain))
                break
        else:
            closed.append(loose_fingerprint_key(field_))
    for name, value in own_project_fields():
        for key in private_keys:
            plain = await decrypt_field(value, key)
            if plain is not None:
                fingerprint.set(name, sha256_of(plain))
                break
        else:
            closed.append(name)
    return fingerprint, closed


def ask_for_keys(
    arguments: argparse.Namespace, *, old_optional: bool = False, generate: bool = False
) -> tuple[Path, str | None, Path, str]:
    """Ask for the two key files and hand back both the answered paths and the private halves.

    The paths come along because the last two actions, renaming and deleting the old key, act on
    a FILE and not on its contents.

    ``old_optional`` is for ``--verify`` alone: step 8 (``--remove-old-key``) DELETES
    ``security/old_key.txt``, while plan and documentation both promise that ``--verify`` still
    works months later. It measures with the new key, so demanding the file the previous step
    removed would turn that promise into exit 2.

    ``generate`` turns the second question around: the new key is MADE at the answered path
    instead of read there, which takes the one hand-typed ``age-keygen`` out of step 1.
    """
    reader = (lambda _question: "") if arguments.ja else input
    old_path = Path(arguments.old_key)
    old_private: str | None = None
    try:
        old_path = ask_for_path("old key", arguments.old_key, reader=reader)
        old_private = read_key(old_path)
    except MissingKey as e:
        if not old_optional:
            raise
        # Print the exception, not a message of our own: ``old_path`` still holds the argparse
        # default here (the class 6d0f2f1d fixed for --rename and --remove-old-key), and a file
        # that IS there but carries no key line fails too, which "no old key at" reported as an
        # absent file.
        print(f"NOTE {e}: checking with the new key alone.")
    if generate:
        new = ask_for_new_key("new key (will be created)", arguments.new_key, reader=reader)
        print(f"New key created: {new}")
    else:
        new = ask_for_path("new key", arguments.new_key, reader=reader)
    return old_path, old_private, new, read_key(new)


@dataclass
class RotationPlan:
    """What is left to do, and what turned out to be done. Both belong on screen."""

    sops: list[Path] = field(default_factory=list)
    loose: list[LooseValue] = field(default_factory=list)
    projects: list[ProjectRound] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    gaps: list[Path] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sops) + len(self.loose) + sum(len(report.fields) for report in self.projects)


async def build_plan(
    paths: list[Path], old_private: str, new_private: str, old_public: str, trees: list[Path] | None = None
) -> RotationPlan:
    """Decide per field whether it still needs converting, is done, or opens with neither key.

    That distinction is measured rather than assumed, and it is what makes a second round
    genuinely idempotent. The SOPS side takes care of itself: it selects on recipient, so a
    converted file falls outside the selection. A loose ``base64+age:`` value does not carry
    its recipient in the text, so there a decryption attempt is the only answer -- without it a
    second round would call the already-converted values "to do" and then strand on the
    fingerprint.
    """
    trees = trees if trees is not None else [REPO]
    plan = RotationPlan(
        sops=[path for tree in trees for path in sops_files_for(tree, old_public)], gaps=coverage_gaps(trees)
    )
    for path in paths:
        for field_ in loose_values(path):
            if await opens_with(field_.value, old_private):
                plan.loose.append(field_)
            elif await opens_with(field_.value, new_private):
                plan.already.append(loose_fingerprint_key(field_))
            else:
                plan.closed.append(loose_fingerprint_key(field_))
    for path in own_project_paths():
        report = await rotate_project_file(path, old_private, new_private, dry_run=True)
        if report.fields:
            plan.projects.append(report)
        elif report.skipped and report.skipped.startswith("already converted"):
            plan.already.append(f"{path}#(project fields)")
        elif report.skipped and not report.skipped.startswith("no encrypted platform fields"):
            plan.closed.append(f"{path}: {report.skipped}")
    return plan


def show_plan(plan: RotationPlan) -> None:
    """Name what would happen, and what is no longer needed."""
    print(f"\n{len(plan.sops)} SOPS files on the old recipient:")
    for path in plan.sops:
        print(f"  sops rotate  {short(path)}")
    print(f"\n{len(plan.loose)} loose encrypted values on the old key:")
    for field_ in plan.loose:
        where = f":{field_.line_number}" if field_.line_number is not None else ""
        print(f"  recrypt      {short(field_.path)}{where}  {field_.name}")
    if plan.projects:
        print(f"\n{len(plan.projects)} project files in this repo's own projects/:")
        for report in plan.projects:
            print(f"  recrypt      {short(report.path)}  {', '.join(report.fields)}")
    for name in plan.already:
        print(f"  already      {name}")
    for name in plan.closed:
        print(f"  FAIL opens with neither key: {name}")
    for path in plan.gaps:
        print(f"  FAIL carries ciphertext and nothing converts it: {short(path)}")
    own = sum(len(report.fields) for report in plan.projects)
    print(
        f"\nTo do: {len(plan.sops)} files + {len(plan.loose)} loose values + {own} project fields = {plan.total} fields"
    )


async def run_rotation(
    plan: RotationPlan, old_private: str, new_private: str, old_public: str, new_public: str
) -> None:
    for path in plan.sops:
        sops_rotate(path, old_public, new_public, old_private)
        print(f"  converted  {short(path)}")
    for field_ in plan.loose:
        conversion = await convert_value(field_.value, old_private, new_public)
        write_loose_value(field_, conversion.new_value)
        where = f":{field_.line_number}" if field_.line_number is not None else ""
        print(f"  converted  {short(field_.path)}{where} {field_.name}")
    for planned in plan.projects:
        report = await rotate_project_file(planned.path, old_private, new_private, dry_run=False)
        if not report.rewritten:
            raise ConversionFailed(f"{planned.path} was not written: {report.skipped}")
        print(f"  converted  {short(report.path)}  {', '.join(report.fields)}")


async def run_final_check(
    old_private: str,
    new_private: str,
    old_public: str,
    new_public: str,
    projects: Path | None,
    expected: int | None,
    trees: list[Path] | None = None,
    argo: Path | None = None,
    pat: str | None = None,
    current_pat: str | None = None,
) -> FinalCheck:
    """A must fail everywhere, B must succeed everywhere, and the count must match.

    Walks the SOPS files on BOTH recipients. Walking only the new one would make a skipped
    file invisible: that one still sits on the old recipient and would fall outside the
    selection.

    On top of the five places it settles the two halves of the coverage list: a file that
    carries ciphertext and is converted by nothing at all (``coverage_gaps()``, over every tree
    this run walks, the argo clone included), and a file on the exception list whose reason turns
    out to be wrong (``check_exceptions``). Without those the verdict is about the fields the
    tool happens to know, not about the old key.

    With ``--projects`` the same inventory runs over that clone (``project_coverage_gaps()``),
    and there it is the only half that does not come out of the walk the round used: a walk and
    a count built from the same glob report CLEAN over everything that glob does not see.
    """
    trees = trees if trees is not None else [REPO]
    check = FinalCheck(expected=expected, token_checked=pat is not None, current_pat_checked=current_pat is not None)
    for path in sops_on_either_recipient(old_public, new_public, trees):
        check_sops_file(path, old_private, new_private, check)
    for path in loose_paths():
        for field_ in loose_values(path):
            await check_value(
                loose_fingerprint_key(field_), field_.value, old_private, new_private, check, pat, current_pat
            )
    for name, value in own_project_fields():
        await check_value(name, value, old_private, new_private, check, pat, current_pat)
    if pat is not None or current_pat is not None:
        # Not counted, for the same reason as the clone's copy below. Why they are checked at
        # all: ``own_plain_passwords``.
        for name, plaintext in own_plain_passwords():
            check_token(name, plaintext, pat, check, current_pat)
    if projects is not None:
        for path in project_files(projects):
            data = load_yaml_from_path(str(path))
            if not isinstance(data, dict):
                continue
            for field_name, value in project_fields(data):
                await check_value(f"{path}#{field_name}", value, old_private, new_private, check, pat, current_pat)
            if pat is not None or current_pat is not None:
                # Outside ``check_value`` and not counted: nothing converted these fields, so
                # counting them would put the total the fingerprint has to match out by exactly
                # their number. Why they are checked at all: ``project_plain_passwords``.
                for field_name, plaintext in project_plain_passwords(data):
                    check_token(f"{path}#{field_name}", plaintext, pat, check, current_pat)
        check.outside_coverage.extend(str(path) for path in project_coverage_gaps(projects))
        if argo is not None:
            await check_repository_secrets(argo, projects, old_private, new_private, check, pat, current_pat)
    check.outside_coverage.extend(short(path) for path in coverage_gaps(trees))
    await check_exceptions(old_private, check)
    return check


async def check_exceptions(old_private: str, check: FinalCheck) -> None:
    """Hold the exception list to its own claim: none of it may open with the old key.

    These files are excused from the conversion because their ciphertext belongs to another
    key -- a sandbox key, a test key. That is a claim written by hand, and the whole point of
    this check is that hand-written coverage is what went wrong. So it is measured here, with
    the one key that can settle it.

    Not counted: these fields were never converted, so they are not in the fingerprint, and
    adding them would make the count that has to match disagree by exactly their number.
    """
    for name in sorted(COVERAGE_EXCEPTIONS):
        path = REPO / name
        if not path.is_file():
            continue
        for candidate in encrypted_candidates(path.read_text(encoding="utf-8")):
            if is_real_ciphertext(candidate) and await opens_with(candidate, old_private):
                check.still_opens_with_old.append(f"{name} (on the exception list: {COVERAGE_EXCEPTIONS[name]})")


async def run_verify(
    fingerprint_path: Path,
    paths: list[Path],
    old_public: str | None,
    new_public: str,
    new_private: str,
    trees: list[Path] | None = None,
    projects: Path | None = None,
    projects_fingerprint: Path | None = None,
) -> int:
    """Check the recorded fingerprints against what the new key reads today.

    With ``--projects`` the fourth place is checked too, against the record
    ``rotate-project-keys.py`` wrote. This mode already validated that flag -- a missing or empty
    directory is exit 2 -- and then walked nothing with it, so the verdict line counted this
    repo's fields alone while the projects fingerprint sat next to it with nothing reading it.
    A number that is written and never read is a tally, not a check.

    Which record belongs to which walk matters here in a way it does not for the count in
    ``--assert-old-key-dead``: that one compares totals, this one compares field NAMES, and a
    name is a path. Every entry point that writes or reads this record therefore resolves the
    directory first, or a clone addressed by a different spelling would report every field as
    disappeared and every field as appeared.
    """
    trees = trees if trees is not None else [REPO]
    if not fingerprint_path.is_file():
        print(f"FAIL no fingerprint to check against: {fingerprint_path}", file=sys.stderr)
        return 2
    if projects is not None and (projects_fingerprint is None or not projects_fingerprint.is_file()):
        print(f"FAIL no fingerprint to check --projects against: {projects_fingerprint}", file=sys.stderr)
        print("That is the record rotate-project-keys.py writes. Leave --projects off to check", file=sys.stderr)
        print("this repo and the argo clone alone.", file=sys.stderr)
        return 2
    wanted = Fingerprint.load(fingerprint_path)
    measured, closed = await fingerprint_now(
        sops_on_either_recipient(old_public, new_public, trees), all_loose_values(paths), new_private
    )
    objections = wanted.compare(measured)
    counted = len(measured.fields)
    if projects is not None and projects_fingerprint is not None:
        measured_projects, closed_projects = await fingerprint_all(projects, new_private)
        objections.extend(Fingerprint.load(projects_fingerprint).compare(measured_projects))
        closed.extend(closed_projects)
        counted += len(measured_projects.fields)
    for name in closed:
        print(f"FAIL does not open with the new key: {name}")
    for objection in objections:
        print(f"FAIL {objection}")
    if objections or closed:
        return 1
    print(f"CLEAN {counted} fields readable with the new key and unchanged in content")
    return 0


def expected_count(paths: list[Path]) -> int | None:
    """The total number of fields across the fingerprints that exist, or None when there is none.

    The final check walks five places, and those were converted by two tools with a
    fingerprint each. The count that has to match is therefore the SUM; passing only one of
    them compares a part against a whole and always yields a deviation.
    """
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return None
    return sum(len(Fingerprint.load(path).fields) for path in existing)


def rename_keys(old: Path, new: Path, *, yes: bool) -> None:
    """Put the keys under their fixed names, so no manual step is left over.

    ``security/key.txt`` always means "this is the key", and the Taskfile, CLAUDE.md and the
    installation documentation reference that path dozens of times. A new key under a new name
    would drag all of those with it, so the old one shifts to ``old_key.txt`` and the new one
    takes over the fixed name.

    ``Path.replace`` overwrites, so a second rotation would land on the previous round's old key
    and destroy the only copy of it. It refuses instead, the way ``generate_key`` refuses to write
    over a key file: while the old key still opens something, losing it is losing the way back.
    """
    if old.resolve() == CANONICAL_OLD.resolve() and new.resolve() == CANONICAL_NEW.resolve():
        print("Keys already sit under their fixed names.")
        return
    if old.resolve() != CANONICAL_OLD.resolve() and CANONICAL_OLD.exists():
        raise KeyExists(
            f"refusing to overwrite an existing file: {CANONICAL_OLD}. That is the previous "
            "rotation's old key. Step 8 (--remove-old-key) deletes it once the final check is "
            "clean; move it aside yourself if that round was never finished."
        )
    print(f"\nRenaming: {old} -> {CANONICAL_OLD} and {new} -> {CANONICAL_NEW}")
    if not yes and input("Do it? [no]: ").strip().lower() not in YES_WORDS:
        print("Not renamed. Do this by hand, or every reference to security/key.txt stays on the old key.")
        return
    if old.resolve() != CANONICAL_OLD.resolve():
        old.replace(CANONICAL_OLD)
    if new.resolve() != CANONICAL_NEW.resolve():
        new.replace(CANONICAL_NEW)
    print("Renamed.")


def run_rename(old: Path, new: Path, *, yes: bool) -> int:
    """The ``--rename`` stand: the refusal has to reach the exit code, not only the traceback."""
    try:
        rename_keys(old, new, yes=yes)
    except KeyExists as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2
    return 0


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
    parser.add_argument(
        "--loose-values-not-on-this-key",
        action="store_true",
        help="the loose values in this repo belong to another recipient; leave them out",
    )
    parser.add_argument("--rename", action="store_true", help="put the keys under their fixed names and stop")
    parser.add_argument(
        "--generate-new-key",
        action="store_true",
        help="make the new key with age-keygen at the answered path, instead of reading it (needs --rename)",
    )
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument("--fingerprint", default=str(DEFAULT_FINGERPRINT))
    parser.add_argument(
        "--projects-fingerprint",
        default=str(DEFAULT_PROJECTS_FINGERPRINT),
        help="the fingerprint rotate-project-keys.py wrote: the final check counts it and --verify compares against it",
    )
    parser.add_argument(
        "--projects",
        help="directory with project files, so the final check and --verify walk the fourth place too",
    )
    parser.add_argument(
        "--pat-new-file",
        "--pat-file",
        dest="pat_new_file",
        help="file holding the new GitHub PAT, so the final check measures the token as well",
    )
    parser.add_argument(
        "--pat-current-file",
        help="file holding the PAT that was replaced; the final check then proves no field"
        " decrypts to it any more, which is the real 'the old PAT is dead' assertion",
    )
    parser.add_argument(
        "--argo-applications",
        help="clone of zad-argo-user-applications: its ArgoCD repository secrets are SOPS files"
        " on the PLATFORM recipient, and the plugin renders them with the secret step 5 replaces",
    )
    return parser


def note_places_left_out(
    projects: Path | None, argo: Path | None, pat: str | None, current_pat: str | None = None
) -> None:
    """Which of the places the final check does not walk, because its flag was left off."""
    if projects is None:
        print("NOTE without --projects the final check does not walk the fourth place.")
    if argo is None:
        print("NOTE without --argo-applications the final check does not walk the ArgoCD")
        print("repository secrets, and those render with the secret step 5 replaces.")
    elif projects is None:
        print("NOTE the ArgoCD repository secrets are only held against their project files")
        print("when --projects is given too; they are DERIVED from those files.")
    if pat is None:
        print("NOTE without --pat-new-file this is a check on the KEY only. A field can sit on")
        print("the new key and still carry the withdrawn token; that half is not measured here.")
    if current_pat is None:
        print("NOTE without --pat-current-file nothing here proves the OLD PAT is gone. The")
        print("token half above recognises a token by its SHAPE; equality needs no shape.")


def note_loose_values_left_out(left_out: bool) -> None:
    """What ``--loose-values-not-on-this-key`` takes off the worklist, said out loud.

    A run that measures less than the usual run has to say so in its own output, because the
    CLEAN line underneath reads the same either way.
    """
    if not left_out:
        return
    print(f"NOTE --loose-values-not-on-this-key: the {len(loose_paths())} loose-value files in this")
    print("repo are left out. Nothing in this run says anything about the values in them.")


def note_argo_left_out_of_the_record(argo: Path | None) -> None:
    """``--argo-applications``, in the run that RECORDS.

    The final check says which place it does not WALK; this run decides which fields the
    fingerprint HOLDS, and every later check reads that record. Saying so only at the final
    check is saying so after the record has been written.
    """
    if argo is None:
        print("NOTE without --argo-applications this run leaves the ArgoCD repository secrets")
        print("out of the conversion AND out of the fingerprint it records.")


async def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    # Only --verify reads without the old key, and only when it is the mode that will run:
    # --rename and --remove-old-key act on the old key FILE, the final check measures with it.
    old_optional = arguments.verify and not (
        arguments.rename or arguments.assert_old_key_dead or arguments.remove_old_key
    )
    if arguments.generate_new_key and not arguments.rename:
        # Rotating onto a freshly made key that still sits under its own name would encrypt
        # every field for a key that nothing in the repo points at: the Taskfile, CLAUDE.md and
        # the install docs all read security/key.txt, and that still holds the old one.
        print("FAIL --generate-new-key goes with --rename: making the key and putting it under", file=sys.stderr)
        print("its fixed name is one step. Without the rename nothing reads the new key.", file=sys.stderr)
        return 2

    try:
        old_path, old_private, new_path, new_private = ask_for_keys(
            arguments, old_optional=old_optional, generate=arguments.generate_new_key
        )
    except (MissingKey, KeyExists) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2

    if (arguments.pat_new_file or arguments.pat_current_file) and not (
        arguments.assert_old_key_dead or arguments.remove_old_key
    ):
        # Reading it anyway and doing nothing with it is worse than refusing: the operator who
        # passed it believes the token was measured, and that belief is what decides whether the
        # old one gets revoked.
        print("FAIL a PAT file belongs to --assert-old-key-dead (or --remove-old-key).", file=sys.stderr)
        print("The converting round replaces no token; replace-git-pat.py does that.", file=sys.stderr)
        return 2
    try:
        pat = read_pat(arguments.pat_new_file) if arguments.pat_new_file else None
        current_pat = read_pat(arguments.pat_current_file) if arguments.pat_current_file else None
    except (MissingKey, OSError) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2

    new_public = public_key_of(new_private)
    # Resolved, not as typed: the projects fingerprint keys off the path of each file.
    projects = Path(arguments.projects).resolve() if arguments.projects else None
    argo = Path(arguments.argo_applications) if arguments.argo_applications else None
    if argo is not None and not argo.is_dir():
        print(f"FAIL no such directory: {argo}", file=sys.stderr)
        return 2
    if projects is not None and not projects.is_dir():
        print(f"FAIL no such directory: {projects}", file=sys.stderr)
        return 2
    if projects is not None and not project_files(projects):
        # The mirror of the line above, and the sharper of the two: a directory that exists but
        # holds no project file makes the final check walk the fourth place over nothing and
        # report CLEAN -- which is the answer the operator deletes the old key on.
        print(f"FAIL no project files under {projects}", file=sys.stderr)
        print("Nothing there matches *.yaml, so the fourth place would be walked over nothing.", file=sys.stderr)
        print("Check the path: the documented one is <clone>/projects.", file=sys.stderr)
        return 2
    trees = sops_trees(argo)
    fingerprint_path = Path(arguments.fingerprint)
    projects_fingerprint = Path(arguments.projects_fingerprint)
    # The SOPS round picks its files by recipient, so pointing this at another key finds that
    # key's files by itself. The loose values cannot do that: outside a SOPS file the text does
    # not say which key it belongs to, so their worklist is a list of PATHS, and those paths hold
    # the platform key's values whatever key is being rotated. Rotating a different recipient --
    # the sandbox key, say -- then trips "opens with neither key" on values that were never in
    # scope. Saying so is the operator's call and has to be explicit, because "not my key" and
    # "I failed to reach it" look identical from here, and the second is what the grendel exists
    # for. ``covered_in`` keeps counting these paths as covered, so this narrows the worklist
    # without opening a hole in ``coverage_gaps()``.
    paths = [] if arguments.loose_values_not_on_this_key else loose_paths()
    note_loose_values_left_out(arguments.loose_values_not_on_this_key)

    if old_private is None:
        # Guaranteed by old_optional above; everything past this point acts on the old key.
        return await run_verify(
            fingerprint_path, paths, None, new_public, new_private, trees, projects, projects_fingerprint
        )

    old_public = public_key_of(old_private)
    if old_public == new_public:
        print("FAIL the old and the new key are the same key", file=sys.stderr)
        return 2

    if arguments.rename:
        return run_rename(old_path, new_path, yes=arguments.ja)

    if arguments.assert_old_key_dead or arguments.remove_old_key:
        note_places_left_out(projects, argo, pat, current_pat)
        fingerprints = [fingerprint_path, projects_fingerprint]
        expected = expected_count(fingerprints) if projects is not None else None
        check = await run_final_check(
            old_private, new_private, old_public, new_public, projects, expected, trees, argo, pat, current_pat
        )
        for line in check.lines():
            print(line)
        if not check.clean:
            return 1
        if arguments.remove_old_key:
            if projects is None:
                print("REFUSED the old key does not go away without --projects: the fourth", file=sys.stderr)
                print("place was not walked, so a project may still sit on the old key.", file=sys.stderr)
                return 1
            if argo is None:
                print("REFUSED the old key does not go away without --argo-applications: the", file=sys.stderr)
                print("ArgoCD repository secrets were not walked, and they sit on this key.", file=sys.stderr)
                return 1
            if old_path.is_file():
                old_path.unlink()
                print(f"Old key removed: {old_path}")
            else:
                print(f"Old key was not there: {old_path}")
        return 0

    if arguments.verify:
        return await run_verify(
            fingerprint_path, paths, old_public, new_public, new_private, trees, projects, projects_fingerprint
        )

    note_argo_left_out_of_the_record(argo)

    plan = await build_plan(paths, old_private, new_private, old_public, trees)
    show_plan(plan)
    if plan.closed:
        print("\nSTOPPED there are fields that open with neither key.", file=sys.stderr)
        return 1
    if plan.gaps:
        # Converting anyway would end in a CLEAN final check over an incomplete walk, which is
        # worse than not converting: it is the answer that says the old key is dead when it is not.
        print("\nSTOPPED a tracked file carries ciphertext that nothing here converts.", file=sys.stderr)
        print("Put it in LOOSE_VALUE_FILES, or on COVERAGE_EXCEPTIONS with the reason.", file=sys.stderr)
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
        sops_on_either_recipient(old_public, new_public, trees), all_loose_values(paths), old_private, new_private
    )
    if closed:
        for name in closed:
            print(f"FAIL opens with neither key: {name}", file=sys.stderr)
        return 1
    # Compared before it is replaced: step 2 run twice, first without --argo-applications and
    # then with it, is two converting runs over one record.
    if replace_record(fingerprint_before, fingerprint_path):
        return 1
    print(f"  {len(fingerprint_before.fields)} fields -> {fingerprint_path}")

    print("\nConverting...")
    await run_rotation(plan, old_private, new_private, old_public, new_public)

    print("\nChecking with the new key...")
    fingerprint_after, closed = await fingerprint_now(
        [path for tree in trees for path in sops_files_for(tree, new_public)], all_loose_values(paths), new_private
    )
    objections = fingerprint_before.compare(fingerprint_after)
    for name in closed:
        print(f"FAIL does not open with the new key: {name}", file=sys.stderr)
    for objection in objections:
        print(f"FAIL {objection}", file=sys.stderr)
    if closed or objections:
        return 1
    print(f"  {len(fingerprint_after.fields)} fields readable with the new key and unchanged in content")

    print("\nDone, and nothing has left this machine yet. Still to do:")
    print("  PREPARE   uv run --project operations-manager/python python scripts/rotate-project-keys.py")
    print("            on a fresh clone of the projects repo, then commit here and in the argo")
    print("            clone without pushing")
    print(
        "  VERIFY-1  uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead"
    )
    print("            --projects <clone>/projects --argo-applications <clone>, and kustomize")
    print("            build over that argo clone")
    print("  APPLY     push all three repos, then")
    print("            uv run --project operations-manager/python python scripts/set-sops-key-secret.py")
    print("  VERIFY-2  that same final check, plus the smoke test in the feature doc")
    return 0
