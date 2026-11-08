#!/usr/bin/env python3
"""Regenerate the ruff ratchet baseline of ``operations-manager/python``.

    python3 scripts/lint_baseline.py              # drop the entries that no longer violate
    python3 scripts/lint_baseline.py --check      # exit 1 when an entry is stale, write nothing
    python3 scripts/lint_baseline.py --initial    # write the baseline once, when there is none

The baseline is ``operations-manager/python/ruff-baseline.toml``: per file the rules that file
still breaks. ``pyproject.toml`` pulls it in with ``extend``, so a rule from
``docs/development/coding-rules.md`` can be switched on repository-wide while the code that
predates it stays green, and new code falls under it at once.

A default run only REMOVES. A rule a file no longer breaks disappears, nothing is ever added,
and adding an entry is a hand edit that shows up in the diff. That is what makes it a ratchet
instead of a list of excuses.

The measurement runs ruff with ``lint.per-file-ignores = {}``, which drops the generated
baseline and keeps the handwritten ``extend-per-file-ignores`` from ``pyproject.toml``: without
that, every file would read as clean because the baseline silenced it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON_DIR = REPO / "operations-manager" / "python"
BASELINE_FILE = PYTHON_DIR / "ruff-baseline.toml"

HEADER = """# GENERATED, do not edit by hand to add anything.
#
# Per file the ruff rules it still breaks. `pyproject.toml` pulls this in with `extend`, so
# these violations are silenced here and nowhere else: new code, and a new file, fall under the
# full rule set from docs/development/coding-rules.md at once.
#
#     task lint-baseline          # regenerate: a rule a file no longer breaks disappears
#
# The task only removes. Adding an entry is a hand edit, and it has to be defended in the PR.
"""


def ruff_violations(python_dir: Path = PYTHON_DIR) -> dict[str, set[str]]:
    """The rules every file breaks right now, with the generated baseline switched off."""
    completed = subprocess.run(  # noqa: S603  # geen shell; de argv is vast, alleen sys.executable varieert
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            "lint.per-file-ignores = {}",
            "--output-format",
            "json",
            ".",
        ],
        cwd=python_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"ruff failed with exit code {completed.returncode}:\n{completed.stderr}")

    violations: dict[str, set[str]] = {}
    for item in json.loads(completed.stdout):
        code = item.get("code")
        if code is None:
            continue
        path = Path(item["filename"]).resolve().relative_to(python_dir.resolve()).as_posix()
        violations.setdefault(path, set()).add(code)
    return violations


def load_baseline(path: Path) -> dict[str, set[str]]:
    """The baseline as it stands, or nothing when the file holds no entries yet."""
    if not path.is_file():
        return {}
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = document.get("lint", {}).get("per-file-ignores", {})
    return {pattern: set(codes) for pattern, codes in entries.items()}


def render_baseline(entries: dict[str, set[str]]) -> str:
    lines = [HEADER, "[lint.per-file-ignores]"]
    for path in sorted(entries):
        codes = ", ".join(f'"{code}"' for code in sorted(entries[path]))
        lines.append(f'"{path}" = [{codes}]')
    return "\n".join(lines) + "\n"


def stale_entries(baseline: dict[str, set[str]], current: dict[str, set[str]]) -> dict[str, set[str]]:
    """Baseline entries that silence nothing: the file is gone, or it no longer breaks that rule.

    Every one of them is a line that reads as if a rule still has to be dealt with somewhere, so
    they are what keeps a cleaned-up file from quietly staying on the list.
    """
    stale: dict[str, set[str]] = {}
    for path, codes in baseline.items():
        gone = codes - current.get(path, set())
        if gone:
            stale[path] = gone
    return stale


def pruned(baseline: dict[str, set[str]], current: dict[str, set[str]]) -> dict[str, set[str]]:
    """The baseline minus everything that no longer violates. Never more than it was."""
    kept: dict[str, set[str]] = {}
    for path, codes in baseline.items():
        still_broken = codes & current.get(path, set())
        if still_broken:
            kept[path] = still_broken
    return kept


def describe(entries: dict[str, set[str]]) -> str:
    per_rule: dict[str, int] = {}
    for codes in entries.values():
        for code in codes:
            per_rule[code] = per_rule.get(code, 0) + 1
    total = sum(per_rule.values())
    return f"{len(entries)} files, {total} rule entries ({len(per_rule)} distinct rules)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report stale entries and exit 1, write nothing")
    parser.add_argument(
        "--initial", action="store_true", help="write the baseline from scratch; refused once it has entries"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    baseline = load_baseline(BASELINE_FILE)
    current = ruff_violations()

    if arguments.initial:
        if baseline:
            print(f"FAIL {BASELINE_FILE.name} already holds {len(baseline)} entries; --initial only writes a new one.")
            print("A baseline never grows by tooling. Run without --initial to prune it, or edit it by hand.")
            return 1
        BASELINE_FILE.write_text(render_baseline(current), encoding="utf-8")
        print(f"Wrote {BASELINE_FILE.name}: {describe(current)}")
        return 0

    stale = stale_entries(baseline, current)
    if arguments.check:
        if not stale:
            print(f"CLEAN every entry in {BASELINE_FILE.name} still silences a violation ({describe(baseline)})")
            return 0
        print(f"FAIL {sum(len(codes) for codes in stale.values())} stale entries in {BASELINE_FILE.name}:")
        for path in sorted(stale):
            print(f"  {path}: {', '.join(sorted(stale[path]))}")
        print("\nThose files no longer break those rules. Run `task lint-baseline` to drop them.")
        return 1

    kept = pruned(baseline, current)
    BASELINE_FILE.write_text(render_baseline(kept), encoding="utf-8")
    removed = sum(len(codes) for codes in stale.values())
    print(f"Wrote {BASELINE_FILE.name}: {describe(kept)}, {removed} entries dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
