#!/usr/bin/env python3
"""Report which components the OOM auto-tune has grown past its ceiling. Read-only.

RC-157 gave the auto-tune an upper bound: an OOM-driven bump may raise a deployment
override to at most ``max_growth_factor`` (8) times the memory limit declared on the
catalog component. That bound only applies from now on -- the components the old,
unbounded escalation already inflated keep the value it left behind.

This tool finds them. Per project it compares, for every deployment component:

* the DECLARED limit -- ``components[].resources.limits.memory``, the anchor;
* the CURRENT limit -- the deployment override if there is one, else the declared value;
* the ``oom-watcher`` entries in that component's ``resources.history``.

It reports the cases whose ratio falls outside the new ceiling. It changes nothing:
no file is written, nothing is committed, no cluster is touched. What to do with the
findings is a decision for a human (see the RC-157 entry in TODO.md).

One caveat about the history column: ``resources.history`` is pruned to five entries,
so a ladder of nine automated steps leaves at most five behind. The count is a lower
bound and a hint about the CAUSE, never the evidence itself -- the ratio is.

Usage:
    cd operations-manager/python

    # a checkout of the zad-projects repo (the directory holding the project YAMLs)
    uv run python scripts/oom_growth_report.py /path/to/zad-projects/projects

    # every component, not just the ones past the ceiling
    uv run python scripts/oom_growth_report.py /path/to/projects --all

    # a different ceiling, to see what a stricter factor would flag
    uv run python scripts/oom_growth_report.py /path/to/projects --factor 4

Or through the Taskfile:

    PROJECTS=/path/to/zad-projects/projects task oom-growth-report
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make ``opi`` importable no matter the working directory: the OPI package lives in
# operations-manager/python, the parent of this scripts/ directory. Without this the
# documented ``uv run python scripts/oom_growth_report.py`` fails with
# ModuleNotFoundError, because Python puts scripts/ (not the package root) on sys.path.
_OPI_ROOT = Path(__file__).resolve().parents[1]
if str(_OPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPI_ROOT))

from opi.services.catalog.resource_tuning.config import resource_tuning_config  # noqa: E402
from opi.services.resource_analyzer import _k8s_memory_to_mb  # noqa: E402  (after sys.path bootstrap)
from ruamel.yaml import YAML, YAMLError  # noqa: E402

#: The history source the auto-tune writes when the change came from an OOM.
OOM_HISTORY_SOURCE = "oom-watcher"


@dataclass(frozen=True)
class Finding:
    """One deployment component measured against its declared limit."""

    project: str
    deployment: str
    component: str
    declared_limit: str
    current_limit: str
    ratio: float
    oom_watcher_entries: int
    over_ceiling: bool

    @property
    def location(self) -> str:
        return f"{self.project}/{self.deployment}/{self.component}"


def _memory_limit(resources: Any) -> str | None:
    """The ``limits.memory`` string of a resources block, or None when absent."""
    if not isinstance(resources, dict):
        return None
    limits = resources.get("limits")
    if not isinstance(limits, dict):
        return None
    memory = limits.get("memory")
    return str(memory) if memory else None


def _count_oom_history(resources: Any) -> int:
    """How many ``oom-watcher`` entries this resources block still carries."""
    if not isinstance(resources, dict):
        return 0
    history = resources.get("history")
    if not isinstance(history, list):
        return 0
    return sum(1 for entry in history if isinstance(entry, dict) and entry.get("source") == OOM_HISTORY_SOURCE)


def analyse_project(project_data: dict[str, Any], factor: float) -> list[Finding]:
    """Measure every deployment component of one project against its declared limit.

    Returns a Finding per component that has a declared limit to measure against;
    components without one are skipped, because a ratio needs a denominator.
    """
    project = str(project_data.get("name", "?"))
    declared: dict[str, str] = {}
    declared_history: dict[str, int] = {}
    for component in project_data.get("components", []) or []:
        name = component.get("name")
        if not name:
            continue
        limit = _memory_limit(component.get("resources"))
        if limit:
            declared[str(name)] = limit
        declared_history[str(name)] = _count_oom_history(component.get("resources"))

    findings: list[Finding] = []
    for deployment in project_data.get("deployments", []) or []:
        deployment_name = str(deployment.get("name", "?"))
        for component in deployment.get("components", []) or []:
            reference = component.get("reference")
            if not reference or str(reference) not in declared:
                continue
            reference = str(reference)
            declared_limit = declared[reference]
            override = _memory_limit(component.get("resources"))
            current_limit = override or declared_limit

            declared_mb = _k8s_memory_to_mb(declared_limit)
            if declared_mb <= 0:
                continue
            ratio = _k8s_memory_to_mb(current_limit) / declared_mb

            findings.append(
                Finding(
                    project=project,
                    deployment=deployment_name,
                    component=reference,
                    declared_limit=declared_limit,
                    current_limit=current_limit,
                    ratio=ratio,
                    oom_watcher_entries=_count_oom_history(component.get("resources"))
                    + declared_history.get(reference, 0),
                    over_ceiling=ratio > factor,
                )
            )
    return findings


def _load_projects(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Parse every project YAML in a directory. Unreadable files are reported, not fatal."""
    yaml = YAML(typ="safe")
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            data = yaml.load(path.read_text(encoding="utf-8"))
        except (YAMLError, OSError) as exc:
            print(f"  ! could not read {path.name}: {exc}", file=sys.stderr)
            continue
        if isinstance(data, dict):
            loaded.append((path, data))
    return loaded


def format_report(findings: list[Finding], factor: float, show_all: bool) -> str:
    """Render the findings, widest ratio first."""
    shown = findings if show_all else [f for f in findings if f.over_ceiling]
    if not shown:
        return f"No component sits above {factor:g}x its declared memory limit."

    shown = sorted(shown, key=lambda f: f.ratio, reverse=True)
    width = max(len(f.location) for f in shown)
    lines = [
        f"{len(shown)} component(s) measured against a ceiling of {factor:g}x the declared limit:",
        "",
        f"{'declared':>9}  {'current':>9}  {'ratio':>6}  {'oom-hist':>8}  component",
    ]
    for finding in shown:
        marker = " <-- above the ceiling" if finding.over_ceiling and show_all else ""
        lines.append(
            f"{finding.declared_limit:>9}  {finding.current_limit:>9}  {finding.ratio:>5.1f}x  "
            f"{finding.oom_watcher_entries:>8}  {finding.location:<{width}}{marker}"
        )
    lines += [
        "",
        "oom-hist counts the oom-watcher entries still in resources.history. That history",
        "is pruned to five entries, so it is a lower bound and a hint about the cause; the",
        "ratio is the measurement. Nothing was changed -- this is a report.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("projects_dir", type=Path, help="directory holding the project YAML files (read-only)")
    parser.add_argument(
        "--factor",
        type=float,
        default=resource_tuning_config().max_growth_factor,
        help="ceiling as a multiple of the declared limit (default: the configured auto-tune ceiling)",
    )
    parser.add_argument("--all", action="store_true", help="show every component, not only the ones above the ceiling")
    args = parser.parse_args(argv)

    if not args.projects_dir.is_dir():
        print(f"Not a directory: {args.projects_dir}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    for _path, project_data in _load_projects(args.projects_dir):
        findings.extend(analyse_project(project_data, args.factor))

    print(format_report(findings, args.factor, args.all))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
