"""Print the test files that belong to the changed files, for `task test-changed`.

The mapping is by name: a changed `opi/x/y.py` picks the test files that import or name
`opi.x.y`, a changed template picks the tests that name the template file, and a changed
test file picks itself. It does not have to be complete: CI runs the whole layer after it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PYTHON_ROOT.parents[1]


def changed_files(base: str) -> list[Path]:
    """Files changed against the base plus everything not yet committed."""
    argv = ["git", "merge-base", base, "HEAD"]
    merge_base = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True)  # noqa: S603
    reference = merge_base.stdout.strip() or "HEAD"
    paths: set[str] = set()
    for command in (["git", "diff", "--name-only", reference], ["git", "status", "--porcelain"]):
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=True)  # noqa: S603
        for line in result.stdout.splitlines():
            paths.add(line[3:] if command[1] == "status" else line)
    return [REPO_ROOT / path for path in sorted(paths) if path]


def needles(path: Path) -> list[str]:
    """The strings a test file would contain if it covers this changed file."""
    try:
        relative = path.relative_to(PYTHON_ROOT)
    except ValueError:
        return []
    if relative.parts[0] == "opi" and relative.suffix == ".py":
        module = relative.with_suffix("")
        parts = module.parts[:-1] if module.name == "__init__" else module.parts
        return [".".join(parts)]
    if relative.parts[0] == "manifests":
        return [relative.name]
    return []


def test_files_for(changes: list[Path]) -> list[Path]:
    tests_root = PYTHON_ROOT / "tests"
    selected = {path for path in changes if path.name.startswith("test_") and path.is_file()}
    wanted = {needle for path in changes for needle in needles(path)}
    if wanted:
        for candidate in tests_root.rglob("test_*.py"):
            source = candidate.read_text(encoding="utf-8", errors="replace")
            if any(needle in source for needle in wanted):
                selected.add(candidate)
    return sorted(path.relative_to(PYTHON_ROOT) for path in selected if path.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="branch to compare against")
    args = parser.parse_args()

    files = test_files_for(changed_files(args.base))
    if not files:
        print("geen toetsbestand gevonden voor de gewijzigde bestanden", file=sys.stderr)
        return 0
    print("\n".join(str(path) for path in files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
