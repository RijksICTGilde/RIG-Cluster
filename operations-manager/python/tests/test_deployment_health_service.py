"""The health check is a system service, and its judgement is asymmetric on purpose.

RC-28 step 2. The check used to be loose platform code in ``opi/services/oom_watcher.py``
while resource-tuning -- which also observes deployments after a sync -- was already a
system service. Same kind of logic, two shapes. It is now ``deployment-health``: the
service owns the judgement, ``oom_watcher`` keeps the observing (kubectl, scheduling,
remediation).

The asymmetry these tests hold is the safety property of the whole feature:

* an observed problem on an application pod is ALWAYS a failure -- no service can talk
  it away, otherwise a stale state hides a real outage;
* the ABSENCE of application pods is the only thing a service's state may explain.
"""

from __future__ import annotations

from opi.services.catalog.base import DeploymentStateFact
from opi.services.catalog.deployment_health import DeploymentHealthService, deployment_health_service
from opi.services.deployment_state import DeploymentState
from opi.services.oom_watcher import PodHealthResult
from opi.services.services_enums import ServiceKind, ServiceType


def _sleeping() -> DeploymentState:
    return DeploymentState(
        facts=[
            DeploymentStateFact(
                service=ServiceType.SLEEP_MODE.value,
                summary="Deze deployment slaapt.",
                expects_no_application_pods=True,
            )
        ]
    )


class TestItIsARegisteredSystemService:
    def test_the_registry_serves_it_and_it_is_a_system_service(self) -> None:
        service = deployment_health_service()

        assert isinstance(service, DeploymentHealthService)
        assert service.definition.kind is ServiceKind.SYSTEM

    def test_a_system_service_is_never_in_a_project_file(self) -> None:
        """Always on: it applies to a project that lists no services at all."""
        assert deployment_health_service().applies_to({"services": []}, "productie") is True


class TestObservedProblemsAlwaysCount:
    """Step 2's verification: the same failures are still found. And the guard the plan
    asks for -- a service must not be able to declare a broken deployment healthy."""

    def test_oom_crashloop_and_image_pull_are_failures(self) -> None:
        service = deployment_health_service()
        state = DeploymentState()

        assert service.counts_as_failure(PodHealthResult("frontend", oom_detected=True), state) is True
        assert service.counts_as_failure(PodHealthResult("frontend", crash_loop_detected=True), state) is True
        assert (
            service.counts_as_failure(PodHealthResult("frontend", image_pull_error="ImagePullBackOff: x"), state)
            is True
        )

    def test_a_clean_observation_is_not_a_failure(self) -> None:
        assert deployment_health_service().counts_as_failure(PodHealthResult("frontend"), DeploymentState()) is False

    def test_a_sleeping_state_does_not_excuse_an_observed_problem(self) -> None:
        """A pod carrying a service role is already excluded by the selector, so anything
        observed here runs as the application. A service claiming the deployment sleeps
        must not make that failure disappear."""
        service = deployment_health_service()

        for observed in (
            PodHealthResult("frontend", oom_detected=True),
            PodHealthResult("frontend", crash_loop_detected=True),
            PodHealthResult("frontend", image_pull_error="ImagePullBackOff: x"),
        ):
            assert service.counts_as_failure(observed, _sleeping()) is True


class TestAbsentPodsAreTheOneThingStateExplains:
    def test_a_sleeping_deployment_explains_its_absent_pods(self) -> None:
        assert deployment_health_service().absent_pods_are_expected(_sleeping()) == "Deze deployment slaapt."

    def test_without_a_claim_absent_pods_stay_unexplained(self) -> None:
        assert deployment_health_service().absent_pods_are_expected(DeploymentState()) is None

    def test_a_fact_that_does_not_claim_zero_pods_explains_nothing(self) -> None:
        """``waking`` reports a situation but expects pods back, so it must not silence
        the "no pods" report."""
        waking = DeploymentState(
            facts=[
                DeploymentStateFact(
                    service=ServiceType.SLEEP_MODE.value,
                    summary="Wordt gewekt.",
                    expects_no_application_pods=False,
                )
            ]
        )

        assert deployment_health_service().absent_pods_are_expected(waking) is None
