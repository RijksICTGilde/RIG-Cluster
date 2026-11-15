#!/usr/bin/env python3
"""Refuse code that breaks a convention ruff cannot see, without reopening what predates it.

    python3 scripts/check-code-conventions.py                     # the gate
    python3 scripts/check-code-conventions.py --update-baseline   # lower the baseline
    python3 scripts/check-code-conventions.py --initial           # write it once, when there is none
    python3 scripts/check-code-conventions.py --report            # every counting line, no verdict

Three counts per file, described in ``code_conventions.py``: Dutch identifiers, Dutch comments
and docstrings, and functions with more comment than code. Exit code 0 when no count is above its
baseline, 1 when one is, so the pre-commit hook and CI can both hang off it.

``scripts/code-conventions-baseline.json`` holds, per file, the counts as they were. A count may
drop and a file may disappear; a count may never rise, and a file that is not in the baseline has
to be at zero. New code therefore meets the rule at once while the ~1600 files that predate it
stay green, and every cleanup round can only make the baseline smaller.

``--update-baseline`` lowers a count to what it is now and drops a file that is gone. It never
raises one: a new violation is a violation, and the way to accept one is a hand edit to the JSON
that shows up in the diff. ``--initial`` writes a baseline from scratch and is refused the moment
there is one, so the ratchet cannot be reset by deleting the file and regenerating it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_conventions import (  # type: ignore[reportMissingImports]
    BASELINE_FILE,
    counts_per_file,
    load_baseline,
    lowered,
    python_files,
    report,
    scan_tree,
    write_baseline,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update-baseline", action="store_true", help="lower the baseline to what the code is now")
    parser.add_argument(
        "--initial", action="store_true", help="write the baseline from scratch; refused once it has entries"
    )
    parser.add_argument("--report", action="store_true", help="print every counting line and no verdict")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    findings = scan_tree()
    scanned = len(python_files())

    if arguments.report:
        for finding in findings:
            print(finding)
        print(f"\n{len(findings)} counting lines in {scanned} files")
        return 0

    baseline = load_baseline(BASELINE_FILE)
    if arguments.initial:
        if baseline:
            print(f"FAIL {BASELINE_FILE.name} already holds {len(baseline)} files; --initial only writes a new one.")
            print("A baseline never grows by tooling. Use --update-baseline to lower it, or edit it by hand.")
            return 1
        current = counts_per_file(findings)
        write_baseline(BASELINE_FILE, current)
        total = sum(sum(per_measure.values()) for per_measure in current.values())
        print(f"Wrote {BASELINE_FILE.name}: {len(current)} files, {total} counting lines")
        return 0

    if arguments.update_baseline:
        updated = lowered(baseline, counts_per_file(findings))
        write_baseline(BASELINE_FILE, updated)
        was = sum(sum(per_measure.values()) for per_measure in baseline.values())
        is_now = sum(sum(per_measure.values()) for per_measure in updated.values())
        print(f"Wrote {BASELINE_FILE.name}: {len(updated)} files, {is_now} counting lines (was {was})")
        return 0

    return report(findings, baseline, scanned)


if __name__ == "__main__":
    raise SystemExit(main())
