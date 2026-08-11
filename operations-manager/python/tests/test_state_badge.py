"""One toestand, one badge (RC-35).

A sleeping deployment showed "Healthy" on the card and "slaapstand" in a block underneath
it. Both were true and together they were unusable, and the reason was that there were two
mechanisms: switched-off replaced the badge through a module of its own, while sleeping
only ever reached the block through the service facts.

Here the badge comes from the facts, so no display knows a service by name. What a fact
says about the card is two things and no more: the word (``badge``) and whether the
application is supposed to have no pods at all -- and the second decides whether the word
takes the place of the green Healthy or stands next to it.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc as templates
from opi.services.catalog.base import DeploymentStateFact
from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, STATE_WAKING, SleepState, write
from opi.services.deployment_state import DeploymentState, collect_deployment_state

TEMPLATE = "project-details/_argocd-deployment-card.html.j2"
CLUSTER = "odcn-production"


def _project(*, sleep_state: str | None = None, disabled: tuple[bool, ...] = (False,)) -> dict:
    project_data: dict = {
        "name": "productie",
        "services": ["sleep-mode"],
        "components": [{"name": f"component-{i}"} for i in range(len(disabled))],
        "deployments": [
            {
                "name": "productie",
                "cluster": CLUSTER,
                "components": [
                    {"reference": f"component-{i}", "image": "ghcr.io/x/y:1", "disabled": flag}
                    for i, flag in enumerate(disabled)
                ],
            }
        ],
    }
    if sleep_state is not None:
        write(project_data, "productie", SleepState(state=sleep_state))
    return project_data


def _render(project_data: dict, *, health: str = "Healthy", state: DeploymentState | None = None) -> str:
    deployment = project_data["deployments"][0]
    if state is None:
        state = collect_deployment_state(project_data, deployment["name"])
    return templates.env.get_template(TEMPLATE).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={deployment["name"]: {"health": health, "sync": "Synced", "errors": []}},
        current_cluster=CLUSTER,
        deployment_states={deployment["name"]: state},
    )


class TestWhatAFactSaysAboutTheCard:
    def test_a_sleeping_deployment_reports_a_badge_word(self) -> None:
        """Step 1 of the plan: the word exists in the fact before any display uses it."""
        state = collect_deployment_state(_project(sleep_state=STATE_SLEEPING), "productie")

        assert state.replacing_badges == ["Slaapstand"]

    def test_a_waking_deployment_puts_no_word_on_the_card(self) -> None:
        """Waking is transitional and its pods are supposed to be coming back, so the
        health verdict is exactly what should stay visible."""
        state = collect_deployment_state(_project(sleep_state=STATE_WAKING), "productie")

        assert state.replacing_badges == []
        assert state.accompanying_badges == []

    def test_a_switched_off_deployment_reports_its_own_word(self) -> None:
        state = collect_deployment_state(_project(disabled=(True, True)), "productie")

        assert state.replacing_badges == ["Uitgeschakeld"]

    def test_partly_off_stands_next_to_the_health_verdict(self) -> None:
        """No ``expects_no_application_pods``, so it never takes the verdict's place: the
        rest of the deployment is still supposed to serve traffic."""
        state = collect_deployment_state(_project(disabled=(True, False, False, False)), "productie")

        assert state.replacing_badges == []
        assert state.accompanying_badges == ["1 van 4 componenten uitgeschakeld"]

    def test_a_fact_without_a_word_stays_off_the_card(self) -> None:
        state = DeploymentState(
            facts=[DeploymentStateFact(service="x", summary="iets", expects_no_application_pods=True)]
        )

        assert state.replacing_badges == []


class TestTheCardDerivesItsBadgeFromTheFacts:
    def test_a_sleeping_deployment_is_not_called_healthy(self) -> None:
        """The complaint this plan starts from: the card said everything was fine and the
        block underneath said the deployment was asleep."""
        html = _render(_project(sleep_state=STATE_SLEEPING))

        assert "Slaapstand" in html
        assert "Healthy" not in html

    def test_a_running_deployment_still_shows_its_health(self) -> None:
        html = _render(_project())

        assert "Healthy" in html
        assert "Slaapstand" not in html

    def test_a_real_failure_survives_a_deployment_being_asleep(self) -> None:
        """The RC-28 rule, unchanged: a state may explain absence, never excuse a
        problem."""
        html = _render(_project(sleep_state=STATE_SLEEPING), health="Degraded")

        assert "Slaapstand" in html
        assert "Degraded" in html

    def test_two_services_reporting_at_once_both_get_their_word(self) -> None:
        """Asleep AND switched off: dropping either would leave the user unable to tell
        whether anything is expected of them, so both are shown -- and in an order that
        does not depend on how the registry happens to be sorted."""
        state = collect_deployment_state(_project(sleep_state=STATE_SLEEPING, disabled=(True,)), "productie")

        assert state.replacing_badges == ["Uitgeschakeld", "Slaapstand"]

        html = _render(_project(sleep_state=STATE_SLEEPING, disabled=(True,)), state=state)
        assert "Uitgeschakeld" in html
        assert "Slaapstand" in html
        assert "Healthy" not in html

    def test_a_badge_is_a_word_and_the_block_keeps_the_sentence(self) -> None:
        """The division the plan asks to settle: a badge has room for a name, not for
        "slaapt sinds gisteren, wordt gewekt bij verkeer". Every fact keeps its summary
        for the block, so nothing that had an explanation loses it to the badge."""
        for project_data in (
            _project(sleep_state=STATE_SLEEPING),
            _project(disabled=(True, True)),
            _project(disabled=(True, False)),
        ):
            for fact in collect_deployment_state(project_data, "productie").facts:
                assert fact.badge is not None
                assert len(fact.badge) <= 40, fact.badge
                assert "." not in fact.badge, "a badge names a situation, it does not explain it"
                assert len(fact.summary) > len(fact.badge), "the block carries what the badge cannot"

    def test_the_card_names_no_service(self) -> None:
        """The point of routing the badge through the facts: the next service that parks a
        deployment needs no second condition in this template."""
        source = templates.env.loader.get_source(templates.env, TEMPLATE)[0]

        # "uitgeschakeld" itself still appears: the alert for a component the watcher
        # switched off after an ImagePullBackOff is about one component, not about the
        # state of the deployment, and it is not a badge.
        for name in ("sleep-mode", "sleep_mode", "slaapstand", "disabled_state"):
            assert name not in source.lower(), f"the card should not know about {name}"
