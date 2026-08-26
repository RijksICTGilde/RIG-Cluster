"""Unit tests for the sleep-mode wake/sleep toggle (``sleep_actions``).

The toggle shows exactly one button, driven by the deployment's sleep state, and nothing
at all when sleep-mode is off for the cluster/project. ``match`` does NOT gate it: that
selects what the sweeper puts to sleep on a deadline, and this button is the manual half.
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


def test_a_deployment_outside_the_match_still_gets_the_button() -> None:
    """``match`` is the sweeper's scope, not the service's gate.

    Gating the button on it too meant that switching sleep-mode on and leaving the match
    field empty -- which is the default -- produced no button on any deployment, with
    nothing anywhere saying why. Nothing in the mechanism needs the scope: the sleep is
    carried out from the stored state, and the sweeper leaves an unmatched deployment
    alone by itself.
    """
    actions = sleep_actions(_project(state=None, match=["OTHER-*"]), "PR-1")

    assert [a.label for a in actions] == ["Deployment slapen"]


def test_an_empty_match_still_gets_the_button() -> None:
    """The case that started this: enabled, no patterns, and the button has to be there."""
    actions = sleep_actions(_project(state=None, match=[]), "PR-1")

    assert [a.label for a in actions] == ["Deployment slapen"]


def test_a_deployment_slept_outside_the_match_can_be_woken() -> None:
    """The other half of the toggle. Without this the sleep button strands a deployment:
    it is asleep, out of scope, and the sweeper never brings it back."""
    actions = sleep_actions(_project(state="sleeping", match=["OTHER-*"]), "PR-1")

    assert [a.label for a in actions] == ["Applicatie wekken"]


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
