"""A switched-off deployment says so, and that is a fact -- not a health verdict (RC-31).

Step 1 of the plan: the fact exists and is collected, before any of the three displays
change. A component with ``disabled: true`` gets ``replicas: 0``, ArgoCD calls zero
replicas healthy, and that green verdict travelled unfiltered to the dashboard banner, the
deployment card and the V2 API. Here the platform starts saying what it actually did.

Three states, not two: one of four components off is a different situation than all four
off, and the tests hold that difference.
"""

from __future__ import annotations

from opi.services.deployment_state import collect_deployment_state
from opi.services.disabled_state import DisabledState, deployment_disabled_state
from opi.services.services_enums import ServiceType


def _project(*disabled_flags: bool | None, definition_flags: tuple[bool | None, ...] = ()) -> dict:
    """A project with one deployment whose components carry the given disabled flags.

    ``None`` means the deployment-component states nothing, so the component definition
    decides -- the precedence the manifest generator uses.
    """
    components: list[dict] = []
    definitions: list[dict] = []
    for index, flag in enumerate(disabled_flags):
        name = f"component-{index}"
        component: dict = {"reference": name, "image": "ghcr.io/x/y:1"}
        if flag is not None:
            component["disabled"] = flag
        components.append(component)

        definition: dict = {"name": name}
        if index < len(definition_flags) and definition_flags[index] is not None:
            definition["disabled"] = definition_flags[index]
        definitions.append(definition)

    return {
        "name": "productie",
        "components": definitions,
        "deployments": [{"name": "productie", "cluster": "odcn-production", "components": components}],
    }


class TestHowMuchOfADeploymentIsOff:
    def test_a_deployment_with_nothing_disabled_is_running(self) -> None:
        state = deployment_disabled_state(_project(False, None), "productie")

        assert state.state is DisabledState.RUNNING
        assert (state.disabled_count, state.total_count) == (0, 2)

    def test_every_component_off_means_the_deployment_is_off(self) -> None:
        state = deployment_disabled_state(_project(True, True), "productie")

        assert state.state is DisabledState.DISABLED
        assert state.is_disabled is True
        assert (state.disabled_count, state.total_count) == (2, 2)

    def test_one_of_several_off_is_partially_disabled(self) -> None:
        """The distinction the plan asks for: something is still serving traffic."""
        state = deployment_disabled_state(_project(True, False, False, False), "productie")

        assert state.state is DisabledState.PARTIALLY_DISABLED
        assert state.is_disabled is False
        assert (state.disabled_count, state.total_count) == (1, 4)

    def test_the_component_definition_decides_when_the_deployment_says_nothing(self) -> None:
        state = deployment_disabled_state(_project(None, definition_flags=(True,)), "productie")

        assert state.state is DisabledState.DISABLED

    def test_an_inline_flag_wins_over_the_definition(self) -> None:
        state = deployment_disabled_state(_project(False, definition_flags=(True,)), "productie")

        assert state.state is DisabledState.RUNNING

    def test_a_deployment_without_components_is_not_called_switched_off(self) -> None:
        """Nothing is off, so "uitgeschakeld" would be a second untruth in place of the
        first."""
        state = deployment_disabled_state(_project(), "productie")

        assert state.state is DisabledState.RUNNING
        assert (state.disabled_count, state.total_count) == (0, 0)

    def test_an_unknown_deployment_yields_an_empty_answer(self) -> None:
        state = deployment_disabled_state(_project(True), "bestaat-niet")

        assert state.state is DisabledState.RUNNING
        assert state.total_count == 0


class TestTheFactReachesTheCollector:
    """Step 1's verification: a deployment with a disabled component reports it, an
    ordinary one does not -- through the same hook every other service uses."""

    def test_a_switched_off_deployment_reports_it_and_expects_no_pods(self) -> None:
        state = collect_deployment_state(_project(True, True), "productie")

        assert [fact.service for fact in state.facts] == [ServiceType.DEPLOYMENT_HEALTH.value]
        assert "staat uit" in state.facts[0].summary
        assert state.expects_no_application_pods is True

    def test_a_partially_switched_off_deployment_still_expects_its_pods(self) -> None:
        """The rest of the deployment is supposed to serve traffic, so absent pods stay
        visible as a problem."""
        state = collect_deployment_state(_project(True, False), "productie")

        assert len(state.facts) == 1
        assert "1 van de 2" in state.facts[0].summary
        assert state.expects_no_application_pods is False

    def test_an_ordinary_deployment_reports_nothing(self) -> None:
        assert collect_deployment_state(_project(False, False), "productie").facts == []

    def test_the_fact_carries_no_health_verdict(self) -> None:
        """The RC-28 shape is kept: "I am off" may never be phrased as "and therefore
        healthy", or a stale flag would hide a real outage."""
        fact = collect_deployment_state(_project(True), "productie").facts[0]

        assert not hasattr(fact, "healthy")
        assert set(fact.details) == {"disabled", "total"}
