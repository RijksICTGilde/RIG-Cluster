"""Tests for dashboard project-health aggregation (Bevinding B2, dashboard).

The dashboard tile keyed on health alone showed green for a project whose render is broken
(ComparisonError -> sync=Unknown, health still Healthy from the last good sync). It must now
reflect the real ArgoCD status.
"""

from opi.web.router import _deployment_dashboard_status, _derive_project_health


def _status(health: str, *, sync: str = "Synced", condition: str | None = None) -> dict:
    data: dict = {"status": {"health": {"status": health}, "sync": {"status": sync}}}
    if condition:
        data["status"]["conditions"] = [{"type": condition, "message": "boom"}]
    return data


class TestDeploymentDashboardStatus:
    def test_comparison_error_is_degraded_even_when_healthy(self):
        # The exact incident shape: healthy last-known, sync Unknown, render broken.
        s = _status("Healthy", sync="Unknown", condition="ComparisonError")
        assert _deployment_dashboard_status(s) == "Degraded"

    def test_healthy_without_condition_stays_healthy(self):
        assert _deployment_dashboard_status(_status("Healthy")) == "Healthy"

    def test_none_is_unknown(self):
        assert _deployment_dashboard_status(None) == "Unknown"

    def test_non_terminal_condition_keeps_health(self):
        s = _status("Healthy", condition="OrphanedResourceWarning")
        assert _deployment_dashboard_status(s) == "Healthy"


class TestDeriveProjectHealth:
    def test_worst_wins_degraded(self):
        assert _derive_project_health(["Healthy", "Degraded", "Progressing"]) == "Degraded"

    def test_progressing_over_healthy(self):
        assert _derive_project_health(["Healthy", "Progressing"]) == "Progressing"

    def test_all_healthy(self):
        assert _derive_project_health(["Healthy", "Healthy"]) == "Healthy"

    def test_empty_is_unknown(self):
        assert _derive_project_health([]) == "Unknown"
