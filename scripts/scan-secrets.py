#!/usr/bin/env python3
"""Refuse a commit, a branch or a history that carries a secret.

    scripts/scan-secrets.py                        # every file git tracks in this tree
    scripts/scan-secrets.py --files a.py b.yaml    # just these, for the pre-commit hook
    scripts/scan-secrets.py --history              # every blob that ever existed (slow)
    scripts/scan-secrets.py --inventory            # also list AGE ciphertext, as an inventory

Exit code 0 when clean, 1 when there is a finding, so CI and the hook can both hang off it.

Why an AGE candidate only counts when ``age-keygen`` accepts it is in ``secret_scan.py``; the
three layers of the guard are in ``features/sops-sleutel-vervangen.md``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secret_scan import (  # type: ignore[reportMissingImports]
    report,
    scan_files,
    scan_history,
    tracked_files,
)

REPO = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", nargs="*", help="scan only these files (the pre-commit hook passes them)")
    parser.add_argument("--history", action="store_true", help="scan every blob in the git history instead")
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="also list AGE ciphertext; committing that is fine, so it is an inventory and not an alarm",
    )
    parser.add_argument("--tree", default=str(REPO), help="the repository to scan (default: this one)")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if shutil.which("age-keygen") is None:
        print("FAIL age-keygen is not on PATH, and without it a real AGE key cannot be told", file=sys.stderr)
        print("apart from a placeholder. Install age and run again.", file=sys.stderr)
        return 2

    tree = Path(arguments.tree)
    if arguments.history:
        findings = scan_history(tree, include_ciphertext=arguments.inventory)
        return report(findings, what=f"the git history of {tree}")

    # "is not None" and not a truthiness test: "--files" with nothing after it would otherwise
    # fall through to the whole-tree scan, which is a surprise in a hook whose whole job is to be
    # fast on the staged files.
    if arguments.files is not None:
        paths = [Path(name) for name in arguments.files]
        findings = scan_files(paths, include_ciphertext=arguments.inventory)
        return report(findings, what=f"{len(paths)} staged files")

    paths = tracked_files(tree)
    findings = scan_files(paths, include_ciphertext=arguments.inventory)
    return report(findings, what=f"{len(paths)} tracked files in {tree}")


if __name__ == "__main__":
    raise SystemExit(main())
