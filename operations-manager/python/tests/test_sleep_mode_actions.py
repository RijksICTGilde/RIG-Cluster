"""Unit tests for the sleep-mode wake/sleep toggle (``sleep_actions``).

The toggle shows exactly one button, driven by the deployment's sleep state, and nothing
at all when sleep-mode is off for the cluster or the deployment is out of ``match`` scope.
"""

from __future__ import annotations

from opi.services.catalog.sleep_mode.actions import sleep_actions


def _project(*, state: str | None = None, match: list[str] | None = None, cluster: str = "sandboxed-local") -> dict:
    deployment: dict = {"name": "PR-1", "cluster": cluster}
    if state is not None:
        deployment["sleep"] = {"state": state}
    return {
        "name": "demo",
        "deployments": [deployment],
        "services": [
            {
                "name": "sleep-mode",
                "config": {"wake-mode": "confirm", "match": match if match is not None else ["PR-1"]},
            }
        ],
    }


def test_awake_offers_sleep_button() -> None:
    actions = sleep_actions(_project(state=None), "PR-1")
    assert len(actions) == 1
    assert actions[0].label == "Deployment slapen"
    assert actions[0].endpoint == "/projects/demo/deployments/PR-1/sleep"


def test_sleeping_offers_wake_button() -> None:
    actions = sleep_actions(_project(state="sleeping"), "PR-1")
    assert len(actions) == 1
    assert actions[0].label == "Applicatie wekken"
    assert actions[0].endpoint == "/projects/demo/deployments/PR-1/wake"


def test_waking_offers_wake_button() -> None:
    actions = sleep_actions(_project(state="waking"), "PR-1")
    assert len(actions) == 1
    assert actions[0].label == "Applicatie wekken"


def test_non_matching_deployment_shows_no_button() -> None:
    assert sleep_actions(_project(state=None, match=["OTHER-*"]), "PR-1") == []


def test_disabled_for_cluster_shows_no_button() -> None:
    # odcn-production is not in the cluster defaults and the project does not enable it.
    project = _project(state=None, cluster="odcn-production")
    project["services"] = [{"name": "sleep-mode", "config": {"enabled": False, "match": ["PR-1"]}}]
    assert sleep_actions(project, "PR-1") == []


def test_broken_config_shows_no_button() -> None:
    # A waker-component naming a non-existent component makes config.load fail loud; the
    # toggle must swallow that and render nothing rather than crash the details page.
    project = _project(state=None)
    project["services"] = [{"name": "sleep-mode", "config": {"match": ["PR-1"], "waker-component": "does-not-exist"}}]
    assert sleep_actions(project, "PR-1") == []


def test_unknown_deployment_shows_no_button() -> None:
    assert sleep_actions(_project(state=None), "GHOST") == []
