#!/usr/bin/env python3
"""Replace the GitHub PAT in every project file -- the same loop, with a different entry.

There is exactly one difference between this tool and ``rotate-project-keys.py``:

    read field -> decrypt with A -> [keep value OR replace it] -> encrypt for B -> write
                                             ^              ^
                                          recrypt       new PAT

So it is the same engine and the same round, and it touches each project file once instead of
twice. ``config.age-private-key`` is only ever re-encrypted here, never replaced; the new PAT
goes into ``repositories[].password``.

    scripts/replace-git-pat.py --projects <clone>/projects --dry-run
    scripts/replace-git-pat.py --projects <clone>/projects

**Hard precondition: the new PAT must already be valid on GitHub before the first file is
written.** A project loses its repository access the moment its file is converted while the
rest is not, so let both tokens be valid on GitHub during the round and revoke the old one only
once everything is over. That costs nothing -- GitHub happily knows two valid tokens at the same
time. It is the one thing a key cannot do, which is why the key rotation has no overlap phase
and this does.

**Advice on ordering.** Build both entry points, but let the first real round do only the key.
Once that has demonstrably gone well, the PAT round is a repeat of something that already
worked. That also keeps the two revertible separately.

**The PAT never comes in as an argument.** It is read from a file or typed without echo, for the
same reason the keys are: a value on the command line lands in the shell history, in the process
table and in every log that records the command.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import MissingKey, ask_for_path, public_key_of, read_key  # type: ignore[reportMissingImports]
from project_rotation import YES_WORDS, broken, report, run_round  # type: ignore[reportMissingImports]

REPO = Path(__file__).resolve().parents[1]
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
DEFAULT_FINGERPRINT = REPO / "security" / "projects-pat-fingerprint.json"


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects", required=True, help="directory holding the project .yaml files")
    parser.add_argument("--pat-file", help="file holding the new PAT; without it you are asked, without echo")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen and stop")
    parser.add_argument("--ja", action="store_true", help="take every default and skip the confirmation")
    parser.add_argument("--no-commit", action="store_true", help="write the files but make no commits")
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument("--fingerprint", default=str(DEFAULT_FINGERPRINT))
    return parser


async def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
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
        return 1 if broken(preview) else 0

    print("\nIs the new PAT ALREADY valid on GitHub, with the old one still valid too?")
    print("If not, every converted project loses its repository access until the round is done.")
    if not arguments.ja and input("Confirm and run? [no]: ").strip().lower() not in YES_WORDS:
        print("Nothing changed.")
        return 0

    result = await run_round(
        directory, old_private, new_private, new_pat=new_pat, dry_run=False, commit=not arguments.no_commit
    )
    report(result, dry_run=False)
    result.fingerprint_before.save(arguments.fingerprint)
    print(f"\nFingerprint of {result.fields} fields -> {arguments.fingerprint}")

    problems = broken(result)
    if problems:
        print(f"\nFAIL {len(problems)} project files were skipped with a real problem.", file=sys.stderr)
        return 1
    if result.fingerprint_before.compare(result.fingerprint_after, replaced=result.replaced_fields):
        return 1

    print("\nDone. Push the clone, check that a project can reach its repository, and only then")
    print("revoke the old PAT on GitHub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
