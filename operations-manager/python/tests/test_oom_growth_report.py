"""Tests for the read-only report on components the old auto-tune inflated.

The report exists because RC-157's ceiling only bounds NEW growth: the components the
unbounded escalation already blew up keep their value. What it must get right is the
ratio -- declared limit as the denominator, the deployment override as the numerator --
and it must stay a report: no file is written, nothing is committed.
"""

from __future__ import annotations

import pytest
from scripts.oom_growth_report import Finding, analyse_project, format_report

FACTOR = 8.0


def _project(*, declared: str, overrides: dict[str, str | None], history: list[dict] | None = None) -> dict:
    """One project with a single component, deployed once per entry in ``overrides``."""
    deployments = []
    for deployment_name, override in overrides.items():
        component: dict = {"reference": "api"}
        if override is not None:
            component["resources"] = {"limits": {"memory": override}, "requests": {"memory": override}}
        if history is not None:
            component.setdefault("resources", {})["history"] = history
        deployments.append(
            {
                "name": deployment_name,
                "cluster": "odcn-production",
                "namespace": "asses-k2n",
                "components": [component],
            }
        )
    return {
        "name": "asses-k2n",
        "components": [
            {"name": "api", "resources": {"limits": {"memory": declared}, "requests": {"memory": declared}}}
        ],
        "deployments": deployments,
    }


def test_flags_the_escalated_case() -> None:
    """45Mi declared, 4096Mi running: 91x, far outside the ceiling."""
    findings = analyse_project(_project(declared="45Mi", overrides={"pr-494": "4096Mi"}), FACTOR)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.over_ceiling is True
    assert finding.ratio == pytest.approx(4096 / 45, rel=1e-3)
    assert finding.declared_limit == "45Mi"
    assert finding.current_limit == "4096Mi"
    assert finding.location == "asses-k2n/pr-494/api"


def test_at_the_ceiling_is_not_flagged() -> None:
    """Exactly 8x is what the tuner is allowed to reach, so it is not a finding."""
    findings = analyse_project(_project(declared="45Mi", overrides={"pr-494": "360Mi"}), FACTOR)

    assert findings[0].ratio == pytest.approx(8.0)
    assert findings[0].over_ceiling is False


def test_without_an_override_the_declared_value_is_the_current_one() -> None:
    """A deployment that never got tuned sits at 1.0x, not at 'unknown'."""
    findings = analyse_project(_project(declared="512Mi", overrides={"productie": None}), FACTOR)

    assert findings[0].current_limit == "512Mi"
    assert findings[0].ratio == pytest.approx(1.0)
    assert findings[0].over_ceiling is False


def test_each_deployment_is_measured_separately() -> None:
    """Two deployments of one component: only the inflated one is flagged."""
    findings = analyse_project(
        _project(declared="45Mi", overrides={"pr-494": "3990Mi", "pr-500": "90Mi"}),
        FACTOR,
    )

    flagged = {f.deployment for f in findings if f.over_ceiling}
    assert flagged == {"pr-494"}


def test_counts_the_oom_watcher_history_entries() -> None:
    """The history column counts oom-watcher entries only, not manual or auto-tune ones."""
    history = [
        {"source": "oom-watcher", "limits": {"memory": "3990Mi"}},
        {"source": "oom-watcher", "limits": {"memory": "2660Mi"}},
        {"source": "manual", "limits": {"memory": "45Mi"}},
        {"source": "auto-tune", "limits": {"memory": "45Mi"}},
    ]
    findings = analyse_project(_project(declared="45Mi", overrides={"pr-494": "3990Mi"}, history=history), FACTOR)

    assert findings[0].oom_watcher_entries == 2


def test_a_component_without_a_declared_limit_is_skipped() -> None:
    """A ratio needs a denominator; without one there is nothing honest to report."""
    project = {
        "name": "asses-k2n",
        "components": [{"name": "api"}],
        "deployments": [
            {
                "name": "pr-494",
                "components": [{"reference": "api", "resources": {"limits": {"memory": "4096Mi"}}}],
            }
        ],
    }

    assert analyse_project(project, FACTOR) == []


def test_report_lists_the_widest_ratio_first() -> None:
    findings = [
        Finding("p", "d1", "a", "45Mi", "360Mi", 8.0, 1, False),
        Finding("p", "d2", "b", "45Mi", "4096Mi", 91.0, 5, True),
        Finding("p", "d3", "c", "100Mi", "1000Mi", 10.0, 2, True),
    ]

    report = format_report(findings, FACTOR, show_all=False)
    lines = [line for line in report.splitlines() if "p/d" in line]

    assert [line.split()[-1] for line in lines] == ["p/d2/b", "p/d3/c"], "widest ratio first, at-ceiling omitted"


def test_report_says_so_when_nothing_is_above_the_ceiling() -> None:
    report = format_report([Finding("p", "d", "a", "45Mi", "90Mi", 2.0, 0, False)], FACTOR, show_all=False)

    assert "No component sits above" in report
