"""A service may contribute state about a deployment; it may never contribute a verdict.

RC-28 step 1. A deployment can be in a situation one service caused -- sleep-mode scales
the application to zero and parks a waker in front of it -- and until now nothing outside
that service knew. Generic code had to infer the situation from the cluster, which is how
a waker's ImagePullBackOff got reported as the component's own failure.

The hook returns FACTS. That is the load-bearing part: if "I am asleep" could be phrased
as "and therefore healthy", a service with a stale state would hide a real outage. The
tests below hold both halves: the fact arrives, and there is no way to say "healthy".
"""

from __future__ import annotations

import dataclasses

from opi.services.catalog.base import DeploymentStateFact, Service
from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, STATE_WAKING, SleepState, write
from opi.services.deployment_state import collect_deployment_state
from opi.services.registry import services_for_hook
from opi.services.services_enums import HookPoint, ServiceType


def _project(sleep_state: str | None = None) -> dict:
    project_data: dict = {
        "name": "productie",
        "services": ["sleep-mode"],
        "components": [{"name": "frontend"}],
        "deployments": [{"name": "productie", "cluster": "odcn-production", "namespace": "productie"}],
    }
    if sleep_state is not None:
        write(project_data, "productie", SleepState(state=sleep_state, expires_at="2026-08-05T12:00:00+00:00"))
    return project_data


class TestSleepModeReportsItsState:
    def test_a_sleeping_deployment_says_it_sleeps_and_expects_no_pods(self) -> None:
        """The plan's step-1 verification: a sleeping deployment reports that it sleeps
        and that zero application pods is the intended state."""
        state = collect_deployment_state(_project(STATE_SLEEPING), "productie")

        assert [fact.service for fact in state.facts] == [ServiceType.SLEEP_MODE.value]
        assert "slaapt" in state.facts[0].summary
        assert state.expects_no_application_pods is True

    def test_waking_reports_the_situation_but_still_expects_pods(self) -> None:
        """The in-between state named in the plan. During a wake the pods are supposed to
        come back, so their absence must stay visible instead of being excused."""
        state = collect_deployment_state(_project(STATE_WAKING), "productie")

        assert len(state.facts) == 1
        assert "gewekt" in state.facts[0].summary
        assert state.expects_no_application_pods is False

    def test_an_awake_deployment_reports_nothing(self) -> None:
        state = collect_deployment_state(_project(), "productie")

        assert state.facts == []
        assert state.expects_no_application_pods is False

    def test_state_is_reported_even_when_the_project_does_not_list_sleep_mode(self) -> None:
        """Sleep-mode can be switched on per cluster without a project selecting it. The
        deployment's recorded state is what counts, so a selection filter here would drop
        the state of exactly the deployments that are asleep."""
        project_data = _project(STATE_SLEEPING)
        project_data["services"] = []

        assert collect_deployment_state(project_data, "productie").expects_no_application_pods is True

    def test_an_unknown_deployment_yields_empty_state(self) -> None:
        assert collect_deployment_state(_project(STATE_SLEEPING), "bestaat-niet").facts == []


class TestAServiceCannotDeclareADeploymentHealthy:
    def test_the_fact_has_no_health_verdict_field(self) -> None:
        """The guard the plan asks for: this test fails the moment someone adds a way for
        a service to call a deployment healthy. The health check must reach that
        conclusion itself, from the facts."""
        field_names = {field.name for field in dataclasses.fields(DeploymentStateFact)}

        assert field_names == {"service", "summary", "expects_no_application_pods", "details"}
        for forbidden in ("healthy", "health", "ok", "verdict", "status"):
            assert forbidden not in field_names

    def test_the_only_consequence_is_about_absent_pods(self) -> None:
        """``expects_no_application_pods`` says the application's pods are meant to be
        absent -- nothing about pods that ARE running. A problem observed on a real
        application pod therefore stays a problem, whatever a service reports."""
        fact = DeploymentStateFact(service="sleep-mode", summary="slaapt", expects_no_application_pods=True)

        assert dataclasses.asdict(fact)["details"] == {}
        assert fact.expects_no_application_pods is True


class TestTheHookIsRegistryDriven:
    def test_participation_is_derived_from_overriding_the_hook(self) -> None:
        """Same rule as AFTER_SYNC: a service is in because it implements the hook, never
        because a list somewhere names it."""
        participants = services_for_hook(HookPoint.DEPLOYMENT_STATE)

        assert ServiceType.SLEEP_MODE in {service.service_type for service in participants}
        assert all(type(service).deployment_state is not Service.deployment_state for service in participants)
