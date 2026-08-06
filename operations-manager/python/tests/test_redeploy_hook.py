"""A rollout wipes the state that described the old content (RC-37).

The report this came from: an image update produced a task that succeeded but no
deployment. The cause was a single hardcoded rule in ``project_manager`` -- it re-enabled
a component only when the disable reason started with an image-pull word -- so a component
the OOM watcher had switched off stayed off, at zero replicas, with nothing saying why.

These tests hold the rule that replaced it: a deliberate rollout (image update, deployment
upsert) clears every state a service recorded about the previous content, whatever the
reason said, and the services do that themselves through ``HookPoint.REDEPLOY``. They also
hold the boundaries -- the cases that must NOT be cleared -- because "clear everything
always" and "clear what this rollout replaced" only differ there.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.base import RedeployContext, Service
from opi.services.catalog.sleep_mode.state import STATE_AWAKE, STATE_SLEEPING, SleepState, read, write
from opi.services.redeploy import run_redeploy_hooks
from opi.services.registry import services_for_hook
from opi.services.services_enums import HookPoint, ServiceType

CLUSTER = "sandboxed-local"  # the cluster where sleep-mode ships enabled


def _project(*, components: list[dict[str, Any]], with_sleep_mode: bool = False) -> dict[str, Any]:
    project: dict[str, Any] = {
        "name": "proj",
        "services": [{"sleep-mode": {"config": {"enabled": True, "match": ["preview-*"]}}}] if with_sleep_mode else [],
        "components": [{"name": comp["reference"]} for comp in components],
        "deployments": [
            {
                "name": "preview-42",
                "cluster": CLUSTER,
                "namespace": "proj",
                "components": components,
            }
        ],
    }
    return project


def _rollout(project: dict[str, Any], component_names: list[str]) -> list[str]:
    """Run the hook the way the rollout paths do, on the deployment WITHIN project."""
    deployment = project["deployments"][0]
    return run_redeploy_hooks("proj", project, deployment, component_names)


def _component(project: dict[str, Any], reference: str) -> dict[str, Any]:
    return next(c for c in project["deployments"][0]["components"] if c["reference"] == reference)


class TestTheHookIsWiredUp:
    def test_the_two_services_that_record_state_answer_the_hook(self) -> None:
        """Both inhabitants are found by scanning the registry, not by name.

        The point of the hook is that ``project_manager`` names nobody; if a service
        stopped answering, the rollout paths would silently clear nothing.
        """
        answering = {service.service_type for service in services_for_hook(HookPoint.REDEPLOY)}

        assert ServiceType.DEPLOYMENT_HEALTH in answering
        assert ServiceType.SLEEP_MODE in answering

    def test_a_service_with_nothing_to_do_with_rollouts_does_not_answer(self) -> None:
        """A hook nobody is forced to answer: the default is silence, not a duty."""
        answering = {service.service_type for service in services_for_hook(HookPoint.REDEPLOY)}

        assert ServiceType.KEYCLOAK not in answering
        assert Service.on_redeploy(Service(), RedeployContext("proj", {}, {})) == []  # type: ignore[abstract]


class TestDisabledComponentsComeBack:
    def test_an_image_pull_disable_is_cleared(self) -> None:
        """The case that already worked before RC-37; it must keep working."""
        project = _project(
            components=[
                {
                    "reference": "web",
                    "image": "reg/app:fixed",
                    "disabled": True,
                    "disabled-reason": "ErrImagePull: manifest unknown (404)",
                    "disabled-image": "reg/app:broken",
                }
            ]
        )

        notices = _rollout(project, ["web"])

        assert _component(project, "web")["disabled"] is False
        assert "disabled-reason" not in _component(project, "web")
        assert len(notices) == 1


class TestAnOomDisableIsClearedToo:
    """The decision the plan left open, answered: EVERY reason is cleared.

    An OOM disable is a judgement about the image that ran out of memory. The new image
    is often exactly the fix for it, and nothing else ever lifts such a disable -- the
    re-enable sweep keys on ``disabled-image``, which only image-pull disables carry -- so
    the component would stay off forever. If the new image OOMs as well, the watcher
    switches it off again, now against the image that really caused it.
    """

    def test_an_oom_disable_is_cleared(self) -> None:
        project = _project(
            components=[
                {
                    "reference": "web",
                    "image": "reg/app:v2",
                    "disabled": True,
                    "disabled-reason": "OOMKilled detected",
                }
            ]
        )

        notices = _rollout(project, ["web"])

        assert _component(project, "web")["disabled"] is False
        assert "uitgeschakeld" in notices[0]
        assert "OOMKilled detected" in notices[0], "the notice must say what was cleared, not just that something was"

    def test_a_crash_loop_disable_is_cleared(self) -> None:
        project = _project(
            components=[
                {
                    "reference": "web",
                    "image": "reg/app:v2",
                    "disabled": True,
                    "disabled-reason": "5 restarts (threshold: 3)",
                }
            ]
        )

        _rollout(project, ["web"])

        assert _component(project, "web")["disabled"] is False


class TestWhatARolloutMustNotClear:
    """The cases that separate "clear what this rollout replaced" from "clear everything"."""

    def test_a_component_this_rollout_did_not_touch_stays_disabled(self) -> None:
        """Pushing a new image for the frontend says nothing about the backend."""
        project = _project(
            components=[
                {"reference": "web", "image": "reg/web:v2"},
                {
                    "reference": "api",
                    "image": "reg/api:v1",
                    "disabled": True,
                    "disabled-reason": "OOMKilled detected",
                },
            ]
        )

        notices = _rollout(project, ["web"])

        assert _component(project, "api")["disabled"] is True
        assert notices == []

    def test_a_project_wide_disable_on_the_component_definition_is_left_alone(self) -> None:
        """``disabled`` on the component DEFINITION is a person's decision over every
        deployment. Rolling out one deployment is not the moment to flip it, and writing
        ``disabled: false`` on this deployment-component would do exactly that for one
        deployment while the switch says off."""
        project = _project(components=[{"reference": "web", "image": "reg/app:v2"}])
        project["components"][0]["disabled"] = True

        notices = _rollout(project, ["web"])

        assert project["components"][0]["disabled"] is True
        assert "disabled" not in _component(project, "web")
        assert notices == []

    def test_a_component_that_was_never_disabled_produces_no_notice(self) -> None:
        project = _project(components=[{"reference": "web", "image": "reg/app:v2"}])

        assert _rollout(project, ["web"]) == []


class TestASleepingDeploymentWakesUp:
    """Also decided rather than left open: sleeping goes to awake, not merely to a later
    deadline. Content rolled out onto a deployment that stays at zero replicas is not
    rolled out at all -- no pod ever picks it up -- so the push would report success while
    the deployment kept running the old thing."""

    def test_a_sleeping_deployment_is_woken_and_says_so(self) -> None:
        project = _project(components=[{"reference": "web", "image": "reg/app:v2"}], with_sleep_mode=True)
        write(project, "preview-42", SleepState(state=STATE_SLEEPING, wake_token="tok"))

        notices = _rollout(project, ["web"])

        assert read(project, "preview-42").state == STATE_AWAKE
        assert any("gewekt" in notice for notice in notices)

    def test_an_awake_deployment_gets_a_fresh_deadline_without_bothering_anyone(self) -> None:
        """The deadline still moves -- that is what keeps an actively developed preview
        awake -- but a deadline moving is not news for the person who pushed."""
        project = _project(components=[{"reference": "web", "image": "reg/app:v2"}], with_sleep_mode=True)
        write(project, "preview-42", SleepState(state=STATE_AWAKE, expires_at="2020-01-01T00:00:00+00:00"))

        notices = _rollout(project, ["web"])

        after = read(project, "preview-42")
        assert after.state == STATE_AWAKE
        assert after.expires_at is not None
        assert after.expires_at > "2020-01-01"
        assert notices == []

    def test_a_deployment_outside_the_match_is_left_asleep(self) -> None:
        """Sleep-mode's own selection still decides; the hook does not override it."""
        project = _project(components=[{"reference": "web", "image": "reg/app:v2"}], with_sleep_mode=True)
        project["deployments"][0]["name"] = "productie"
        write(project, "productie", SleepState(state=STATE_SLEEPING))

        notices = _rollout(project, ["web"])

        assert read(project, "productie").state == STATE_SLEEPING
        assert notices == []

    def test_sleep_mode_off_means_nothing_happens(self) -> None:
        project = _project(components=[{"reference": "web", "image": "reg/app:v2"}])

        assert _rollout(project, ["web"]) == []
        assert "sleep" not in project["deployments"][0]


class TestBothServicesActOnTheSameRollout:
    def test_a_sleeping_deployment_with_a_disabled_component_gets_both_cleared(self) -> None:
        """Two services record state about one deployment, and one rollout clears both --
        in one pass, so the caller commits once."""
        project = _project(
            components=[
                {
                    "reference": "web",
                    "image": "reg/app:v2",
                    "disabled": True,
                    "disabled-reason": "OOMKilled detected",
                }
            ],
            with_sleep_mode=True,
        )
        write(project, "preview-42", SleepState(state=STATE_SLEEPING))

        notices = _rollout(project, ["web"])

        assert _component(project, "web")["disabled"] is False
        assert read(project, "preview-42").state == STATE_AWAKE
        assert len(notices) == 2
