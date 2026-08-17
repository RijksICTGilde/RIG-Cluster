#!/usr/bin/env python3
"""RC-19 Layer 2: summarize what a zad-deployments upgrade removed, per project.

The upgrade-safety test's mechanical yardstick is the zad-deployments repo -- it
holds everything OPI generates for a project (manifests, secrets, configmaps, RBAC,
network policies). The test:

1. brings up the sandbox on the OLD OPI image, provisions the sample projects, and
   records the zad-deployments commit at that point (the baseline);
2. swaps in the NEW OPI image and re-processes every project;
3. diffs zad-deployments against the baseline.

Every disappeared env var, secret key, ingress, mount or schema shows up as a
removed line. This tool turns that raw ``git diff`` into a per-project summary of
what disappeared, so "raakt iemand iets kwijt" becomes a short readable list
instead of a wall of diff.

A difference is not automatically a bug: this release changes some things on
purpose (the one-off migration to v2.6). The output is therefore a report to be
JUDGED -- each removal is either explained and wanted, or it is a regression.

Usage:
    # Record the baseline BEFORE swapping the OPI image (in the zad-deployments checkout):
    git -C /path/to/zad-deployments rev-parse HEAD

    # After the upgrade + re-process, summarize what changed since that baseline:
    uv run python scripts/compare_deployments_diff.py /path/to/zad-deployments <baseline-sha>

    # Compare two explicit refs:
    uv run python scripts/compare_deployments_diff.py /path/to/zad-deployments <baseline> <target>

    # Summarize a diff captured elsewhere (no git needed):
    git -C /path/to/zad-deployments diff <baseline> | \
        uv run python scripts/compare_deployments_diff.py --stdin
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field

# A "diff --git a/<pathA> b/<pathB>" header. We take pathB (the new path); for a
# deleted file pathB is still the original path in git's a/ b/ header.
_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")

# Categorization of a removed line. Order matters: first match wins, most specific first.
_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("resource", re.compile(r"^\s*kind:\s*(?P<v>[A-Za-z]+)\s*$")),
    ("schema", re.compile(r"(?i)(search_path|schema)")),
    ("mount", re.compile(r"(?i)(mountpath|mount-path)")),
    ("ingress-host", re.compile(r"^\s*host:\s*\S")),
    ("env-var", re.compile(r"^\s*-?\s*name:\s*(?P<v>[A-Z][A-Z0-9_]{2,})\s*$")),
    ("data-key", re.compile(r"^\s*(?P<v>[A-Z][A-Z0-9_]{2,}):")),
]


@dataclass
class ProjectDiff:
    """Net removals for one ``cluster/project`` group."""

    key: str
    #: category -> removed lines (stripped), in first-seen order.
    removed_by_category: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.removed_by_category.values())


def _project_key(path: str) -> str:
    """Group key for a manifest path: ``cluster/project`` (the first two segments).

    The zad-deployments layout is ``<cluster>/<project>/<deployment>/<file>``; the
    first two segments identify the project unambiguously and read well in a report.
    Shorter paths fall back to whatever is there.
    """
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[:2]) if len(parts) >= 2 else path


def _categorize(line: str) -> str:
    for name, pattern in _CATEGORIES:
        if pattern.search(line):
            return name
    return "other"


_NAMED_ITEM = re.compile(r"^-?\s*name:\s*(?P<v>\S+)\s*$")


def _identity(line: str) -> str:
    """A stable identity for a manifest line, so a value change is not read as a removal.

    - A named list item (``- name: FOO`` / ``name: FOO``, e.g. an env var) is identified
      by its name value: renaming it IS a real disappearance of the old name.
    - Any other ``key: value`` line is identified by its key, so changing only the value
      keeps the same identity and is not reported.
    - A structural line without ``:`` is identified by its whole text.
    """
    stripped = line.strip()
    named = _NAMED_ITEM.match(stripped)
    if named:
        return f"name={named.group('v')}"
    if ":" in stripped:
        return stripped.split(":", 1)[0].lstrip("- ").strip()
    return stripped


def summarize_diff(diff_text: str) -> dict[str, ProjectDiff]:
    """Turn a unified ``git diff`` into per-project net removals by category.

    "Net" removals filter out pure value changes and reordering: a line removed in a
    file but also added back in the same file (a changed value keeps the key on both
    sides) is not a disappearance. Only keys/lines that are gone after the change are
    reported.
    """
    # Per file: removed and added content lines, so we can take the net difference.
    per_file_removed: dict[str, list[str]] = {}
    per_file_added: dict[str, Counter[str]] = {}
    file_order: list[str] = []
    current: str | None = None

    for line in diff_text.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            current = header.group("b")
            if current not in per_file_removed:
                per_file_removed[current] = []
                per_file_added[current] = Counter()
                file_order.append(current)
            continue
        if current is None:
            continue
        # Skip the +++/--- file markers; only real content +/- lines count.
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("-"):
            per_file_removed[current].append(line[1:])
        elif line.startswith("+"):
            per_file_added[current][line[1:]] += 1

    result: dict[str, ProjectDiff] = {}
    for path in file_order:
        # Identities present on the added side, so a removed line whose identity is
        # re-added (a value change, a reorder) is not counted as a disappearance.
        added_identities: Counter[str] = Counter(_identity(added) for added in per_file_added[path].elements())
        for raw in per_file_removed[path]:
            stripped = raw.strip()
            if not stripped:
                continue
            identity = _identity(raw)
            if added_identities.get(identity, 0) > 0:
                added_identities[identity] -= 1
                continue
            key = _project_key(path)
            project = result.setdefault(key, ProjectDiff(key=key))
            category = _categorize(raw)
            project.removed_by_category.setdefault(category, []).append(stripped)

    return result


def format_report(summary: dict[str, ProjectDiff]) -> str:
    """Render the per-project summary as a human-readable report."""
    if not summary:
        return "No removals detected in the diff -- nothing disappeared from zad-deployments."

    lines: list[str] = []
    for key in sorted(summary):
        project = summary[key]
        lines.append(f"\n## {key}  ({project.total} removed)")
        for category in sorted(project.removed_by_category):
            items = project.removed_by_category[category]
            lines.append(f"  {category} ({len(items)}):")
            lines.extend(f"    - {item}" for item in items)
    lines.append("")
    lines.append(
        "Each removal is either explained by this release (e.g. the one-off v2.6 migration) "
        "and wanted, or it is a regression. Judge every line."
    )
    return "\n".join(lines)


def _git_diff(repo: str, baseline: str, target: str) -> str:
    proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        ["git", "-C", repo, "diff", f"{baseline}..{target}"],  # noqa: S607 (git resolved from PATH)
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Summarize zad-deployments removals per project (RC-19 Layer 2)")
    parser.add_argument("repo", nargs="?", help="Path to the zad-deployments checkout")
    parser.add_argument("baseline", nargs="?", help="Baseline git ref recorded before the OPI image swap")
    parser.add_argument("target", nargs="?", default="HEAD", help="Target ref to compare against (default HEAD)")
    parser.add_argument("--stdin", action="store_true", help="Read a unified diff from stdin instead of running git")
    args = parser.parse_args(argv)

    if args.stdin:
        diff_text = sys.stdin.read()
    else:
        if not args.repo or not args.baseline:
            parser.error("repo and baseline are required unless --stdin is given")
        try:
            diff_text = _git_diff(args.repo, args.baseline, args.target)
        except subprocess.CalledProcessError as exc:
            print(f"git diff failed: {exc.stderr.strip()}", file=sys.stderr)
            return 1

    print(format_report(summarize_diff(diff_text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
