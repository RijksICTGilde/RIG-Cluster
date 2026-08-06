"""deployment-health service package (RC-28).

A system service (``kind=SYSTEM``): never in a project's services list, not selectable.
It owns the *judgement* over a running deployment -- what an observed pod state means --
while the observing itself (the kubectl calls, the scheduling, the remediation) stays in
``opi/services/oom_watcher.py``. That is the same split resource-tuning already has: the
service is the declarative home of the decision, the business module does the work.

Why it is a service at all: the check has to weigh what OTHER services report about a
deployment (``HookPoint.DEPLOYMENT_STATE``), and "a check that consults the registry"
belongs in the catalog next to the services it consults, not as loose platform code.

The judgement is deliberately asymmetric, and that asymmetry is the safety property:

* An observed problem on an application pod is ALWAYS a failure. No reported state
  excuses it -- otherwise a service with a stale state hides a real outage.
* The absence of application pods is the only thing a service's state can explain,
  because a service that scaled the application to zero is the one that knows why.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.services.catalog.base import DeploymentStateContext, DeploymentStateFact, Service
from opi.services.disabled_state import deployment_disabled_state
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.services.deployment_state import DeploymentState
    from opi.services.oom_watcher import PodHealthResult


class DeploymentHealthService(Service):
    service_type = ServiceType.DEPLOYMENT_HEALTH

    def deployment_state(self, ctx: DeploymentStateContext) -> list[DeploymentStateFact]:
        """Report that this deployment is switched off, wholly or in part (RC-31).

        ``disabled: true`` is a field on a component, so no ordinary service owns it --
        and that is exactly why it is reported here rather than through a second,
        hook-bypassing path in ``collect_deployment_state``. The hook is the contract
        ("who knows something about this deployment, say so"); a generic contribution
        alongside it would mean two ways to add a fact and two places to look for one.
        This service is the system service that already speaks for the platform itself,
        so the platform's own fact belongs to it.

        Facts, not verdicts, and the split matters here as much as it did for sleep-mode:

        * fully off -- nothing of the application is meant to be running, so
          ``expects_no_application_pods``. It excuses ABSENT pods and nothing else: a
          component that is off and also broken keeps reporting broken.
        * partly off -- the rest is still supposed to serve traffic, so the absence of
          pods is NOT excused; the fact only carries the counts so a reader sees what
          is off.
        """
        state = deployment_disabled_state(ctx.project_data, ctx.deployment_name)
        if state.is_disabled:
            return [
                DeploymentStateFact(
                    service=self.service_type.value,
                    summary=(
                        "Deze deployment staat uit: alle "
                        f"{state.total_count} componenten zijn uitgeschakeld en blijven uit "
                        "tot iemand ze weer aanzet."
                    ),
                    expects_no_application_pods=True,
                    details={"disabled": state.disabled_count, "total": state.total_count},
                )
            ]
        if state.is_partially_disabled:
            return [
                DeploymentStateFact(
                    service=self.service_type.value,
                    summary=(
                        f"{state.disabled_count} van de {state.total_count} componenten van deze "
                        "deployment zijn uitgeschakeld; de rest draait gewoon."
                    ),
                    expects_no_application_pods=False,
                    details={"disabled": state.disabled_count, "total": state.total_count},
                )
            ]
        return []

    def counts_as_failure(self, health: PodHealthResult, state: DeploymentState) -> bool:
        """Whether an observed pod problem is a failure of the application.

        ``state`` is accepted and deliberately not consulted: a problem observed here is
        observed on an application pod (pods a service runs alongside are excluded by
        ``application_pod_selector``), so no service may talk it away. Taking the
        parameter and ignoring it is the point -- it documents at the call site that the
        state was available and did not get a vote.
        """
        return bool(health.oom_detected or health.image_pull_error or health.crash_loop_detected)

    def absent_pods_are_expected(self, state: DeploymentState) -> str | None:
        """The reason a component legitimately has no application pods, or None.

        ``None`` means nobody claims responsibility for the silence, so zero pods is
        something the user should see (starting up, or not coming up at all). A reason
        means a service says it scaled the application to zero on purpose, and the
        sentence it returns is that service's own wording.
        """
        for fact in state.facts:
            if fact.expects_no_application_pods:
                return fact.summary
        return None


def deployment_health_service() -> DeploymentHealthService:
    """The registered deployment-health service, typed.

    Callers go through the registry (one instance, one source of truth) but need the
    concrete type to reach the judgement methods; this is that one narrowing spot instead
    of a cast at every call site. The import is lazy because the registry imports this
    module.
    """
    from opi.services.registry import get_service

    service = get_service(ServiceType.DEPLOYMENT_HEALTH)
    if not isinstance(service, DeploymentHealthService):
        raise TypeError(f"deployment-health is registered as {type(service).__name__}")
    return service
