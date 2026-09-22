#!/usr/bin/env python3
"""Move the project files to the new platform key: two fields per file, one commit per project.

This is the fourth and largest place the platform key occurs: 45 files in the projects repo,
each with TWO fields on the platform key -- ``config.age-private-key`` and every
``repositories[].password``. A project where only the first was converted can no longer reach
its own repository, which is why both always go together.

    scripts/rotate-project-keys.py --projects <clone>/projects --dry-run
    scripts/rotate-project-keys.py --projects <clone>/projects

Point it at a LOCAL CLONE of the projects repo. It writes and commits there and pushes
nothing: the cutover verifies while nothing has been pushed yet, and then the operator pushes
the clone. See ``project_rotation.py`` for why this does not go through
``save_and_commit_project``.

**Never with sed, awk or str.replace.** Measured while the plan was being written: a text
replacement over these 45 files left 56 of the 90 fields silently on the old key, without an
error. ``age-private-key`` is a multi-line YAML block scalar where the indentation and the
chomping indicator have to be exact, ``repositories[].password`` is a single line in the same
file, and 3 of the 45 carry comments a naive YAML round trip throws away. This tool loads and
writes through ``opi.utils.yaml_util``, the canonical round-trip writer.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import MissingKey, ask_for_path, public_key_of, read_key  # type: ignore[reportMissingImports]
from project_rotation import YES_WORDS, broken, report, run_round  # type: ignore[reportMissingImports]

REPO = Path(__file__).resolve().parents[1]
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
DEFAULT_FINGERPRINT = REPO / "security" / "projects-fingerprint.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects", required=True, help="directory holding the project .yaml files")
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
    result.fingerprint_before.save(arguments.fingerprint)
    print(f"\nFingerprint of {result.fields} fields -> {arguments.fingerprint}")

    problems = broken(result)
    if problems:
        print(f"\nFAIL {len(problems)} project files were skipped with a real problem.", file=sys.stderr)
        return 1
    if result.fingerprint_before.compare(result.fingerprint_after):
        return 1

    print(f"\nDone. Check `git log --oneline` and `git diff --stat HEAD~{len(result.committed)}` in the clone,")
    print(f"then push. After that: scripts/rotate-sops-key.py --assert-old-key-dead --projects {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
