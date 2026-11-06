"""Tests for dashboard project-health aggregation (Bevinding B2, dashboard).

The dashboard tile keyed on health alone showed green for a project whose render is broken
(ComparisonError -> sync=Unknown, health still Healthy from the last good sync). It must now
reflect the real ArgoCD status.
"""

from datetime import UTC, datetime, timedelta

from opi.web.router import _deployment_dashboard_status, _derive_project_health


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


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

    def test_progressing_for_weeks_is_degraded(self):
        # The production shape: a rescheduled pod in ImagePullBackOff after a finished
        # rollout keeps Progressing=True, so ArgoCD never says Degraded.
        s = _status("Progressing")
        s["status"]["health"]["lastTransitionTime"] = _ago(20 * 24 * 3600)
        assert _deployment_dashboard_status(s) == "Degraded"

    def test_recent_progressing_stays_progressing(self):
        s = _status("Progressing")
        s["status"]["health"]["lastTransitionTime"] = _ago(30)
        assert _deployment_dashboard_status(s) == "Progressing"

    def test_progressing_without_transition_time_stays_progressing(self):
        assert _deployment_dashboard_status(_status("Progressing")) == "Progressing"

    def test_failed_sync_is_degraded(self):
        s = _status("Progressing", sync="OutOfSync")
        s["status"]["operationState"] = {"phase": "Failed", "message": "one or more objects failed to apply"}
        assert _deployment_dashboard_status(s) == "Degraded"

    def test_stuck_running_sync_is_degraded(self):
        s = _status("Progressing", sync="OutOfSync")
        s["status"]["operationState"] = {"phase": "Running", "startedAt": _ago(3600)}
        s["operation"] = {"sync": {}}
        assert _deployment_dashboard_status(s) == "Degraded"

    def test_young_running_sync_stays_progressing(self):
        s = _status("Progressing", sync="OutOfSync")
        s["status"]["operationState"] = {"phase": "Running", "startedAt": _ago(20)}
        s["operation"] = {"sync": {}}
        assert _deployment_dashboard_status(s) == "Progressing"


class TestDeriveProjectHealth:
    def test_worst_wins_degraded(self):
        assert _derive_project_health(["Healthy", "Degraded", "Progressing"]) == "Degraded"

    def test_progressing_over_healthy(self):
        assert _derive_project_health(["Healthy", "Progressing"]) == "Progressing"

    def test_all_healthy(self):
        assert _derive_project_health(["Healthy", "Healthy"]) == "Healthy"

    def test_empty_is_unknown(self):
        assert _derive_project_health([]) == "Unknown"
