"""Unit tests for the sleep-mode sweep planning and the wake action."""

from datetime import UTC, datetime, timedelta

from opi.services.catalog.sleep_mode.actions import sleep_actions
from opi.services.catalog.sleep_mode.scheduler import CHECK_AWAKE, REVERT, SLEEP, STAMP, decide_action, plan_sweep

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
PAST = (NOW - timedelta(minutes=1)).isoformat()
FUTURE = (NOW + timedelta(hours=1)).isoformat()


class TestDecideAction:
    def test_awake_matching_expired_sleeps(self) -> None:
        assert decide_action("awake", PAST, matches=True, now=NOW) == SLEEP

    def test_awake_not_expired_no_action(self) -> None:
        assert decide_action("awake", FUTURE, matches=True, now=NOW) is None

    def test_awake_not_matching_no_action(self) -> None:
        assert decide_action("awake", PAST, matches=False, now=NOW) is None

    def test_awake_without_deadline_stamps(self) -> None:
        # A matching awake deployment with no deadline yet gets one stamped.
        assert decide_action("awake", None, matches=True, now=NOW) == STAMP

    def test_awake_without_deadline_not_matching_no_action(self) -> None:
        assert decide_action("awake", None, matches=False, now=NOW) is None

    def test_waking_expired_reverts(self) -> None:
        assert decide_action("waking", PAST, matches=True, now=NOW) == REVERT

    def test_waking_not_expired_checks_app(self) -> None:
        # Not timed out yet: check whether the app is back so we can finish promptly.
        assert decide_action("waking", FUTURE, matches=True, now=NOW) == CHECK_AWAKE

    def test_sleeping_no_action(self) -> None:
        assert decide_action("sleeping", PAST, matches=True, now=NOW) is None


class TestPlanSweep:
    def _project(self, deployments: list[dict]) -> dict:
        return {
            "name": "proj",
            "services": [{"name": "sleep-mode", "config": {"enabled": True, "match": ["PR-*"]}}],
            "deployments": deployments,
        }

    def test_expired_matching_deployment_planned_for_sleep(self) -> None:
        project = self._project([{"name": "PR-1", "cluster": "local", "sleep": {"state": "awake", "expires-at": PAST}}])
        assert plan_sweep(project, "local", NOW) == [("PR-1", SLEEP)]

    def test_non_matching_deployment_skipped(self) -> None:
        project = self._project([{"name": "main", "cluster": "local", "sleep": {"state": "awake", "expires-at": PAST}}])
        assert plan_sweep(project, "local", NOW) == []

    def test_other_cluster_skipped(self) -> None:
        project = self._project(
            [{"name": "PR-1", "cluster": "odcn-production", "sleep": {"state": "awake", "expires-at": PAST}}]
        )
        assert plan_sweep(project, "local", NOW) == []

    def test_stuck_waking_planned_for_revert(self) -> None:
        project = self._project(
            [{"name": "PR-1", "cluster": "local", "sleep": {"state": "waking", "expires-at": PAST}}]
        )
        assert plan_sweep(project, "local", NOW) == [("PR-1", REVERT)]

    def test_sleep_mode_off_returns_empty(self) -> None:
        project = {
            "name": "proj",
            "services": [],
            "deployments": [{"name": "PR-1", "cluster": "local", "sleep": {"state": "awake", "expires-at": PAST}}],
        }
        assert plan_sweep(project, "local", NOW) == []


class TestSleepActions:
    # The toggle now needs sleep-mode enabled + the deployment in scope; the awake/sleeping
    # split is exercised in depth in test_sleep_mode_actions.py.
    @staticmethod
    def _project(state: str | None) -> dict:
        deployment: dict = {"name": "PR-1", "cluster": "sandboxed-local"}
        if state is not None:
            deployment["sleep"] = {"state": state}
        return {
            "name": "proj",
            "deployments": [deployment],
            "services": [{"name": "sleep-mode", "config": {"match": ["PR-1"]}}],
        }

    def test_wake_button_when_not_awake(self) -> None:
        actions = sleep_actions(self._project("sleeping"), "PR-1")
        assert len(actions) == 1
        assert actions[0].label == "Applicatie wekken"
        assert actions[0].endpoint == "/projects/proj/deployments/PR-1/wake"
        assert actions[0].kind == "primary"

    def test_sleep_button_when_awake(self) -> None:
        actions = sleep_actions(self._project(None), "PR-1")
        assert len(actions) == 1
        assert actions[0].label == "Deployment slapen"
        assert actions[0].endpoint == "/projects/proj/deployments/PR-1/sleep"
