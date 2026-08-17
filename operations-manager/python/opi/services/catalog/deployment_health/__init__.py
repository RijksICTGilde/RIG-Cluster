"""deployment-health service package (RC-28).

A system service (``kind=SYSTEM``): never in a project's services list, not selectable.
It owns the *judgement* over a running deployment -- what an observed pod state means --
while the observing itself (the kubectl calls, the scheduling, the remediation) stays in
``opi/services/oom_watcher.py``. That is the same split resource-tuning already has: the
service is the declarative home of the decision, the business module does the work.

Why it is a service at all: the check has to weigh what OTHER services report about a
deployment (``UIEvent.DEPLOYMENT_STATE``), and "a check that consults the registry"
belongs in the catalog next to the services it consults, not as loose platform code.

The judgement is deliberately asymmetric, and that asymmetry is the safety property:

* An observed problem on an application pod is ALWAYS a failure. No reported state
  excuses it -- otherwise a service with a stale state hides a real outage.
* The absence of application pods is the only thing a service's state can explain,
  because a service that scaled the application to zero is the one that knows why.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.services.catalog.base import DeploymentStateContext, DeploymentStateFact, RedeployContext, Service
from opi.services.catalog.deployment_health.disabled import deployment_disabled_state
from opi.services.catalog.events import on
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ActionEvent, ServiceBinding, ServiceKind, ServiceType, UIEvent

if TYPE_CHECKING:
    from opi.services.deployment_state import DeploymentState
    from opi.services.oom_watcher import PodHealthResult


class DeploymentHealthService(Service):
    service_type = ServiceType.DEPLOYMENT_HEALTH
    definition = ServiceDefinition(
        name="Deployment gezondheid",
        description=(
            "Systeemdienst: beoordeelt wat de waargenomen toestand van een draaiende "
            "deployment betekent (OOM, CrashLoopBackOff, image ophalen mislukt) en weegt "
            "daarbij mee wat andere diensten over die deployment melden. Draait altijd, "
            "is niet kiesbaar."
        ),
        help_template="deployment_health/help.md",
        icon="stethoscoop",
        color="grijs-600",
        binding=ServiceBinding.DEPLOYMENT,
        variables=[],
        # Always on, never in the project file -> a system service (kind=SYSTEM also
        # keeps it out of the picker, so no explicit hidden is needed).
        kind=ServiceKind.SYSTEM,
    )
    # May enrol itself (RC-84): a system service is not a user choice, so there is no
    # project-level decision to make first.
    allows_implicit_project_selection = True

    @on(UIEvent.DEPLOYMENT_STATE)
    def report_disabled_state(self, ctx: DeploymentStateContext) -> list[DeploymentStateFact]:
        """Report that this deployment is switched off, wholly or in part (RC-31).

        ``disabled: true`` is a field on a component, so no ordinary service owns it --
        and that is exactly why it is reported here rather than through a second,
        event-bypassing path in ``collect_deployment_state``. The event is the contract
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
                        "Deze deployment staat uit: "
                        + (
                            "het component is uitgeschakeld en blijft uit tot iemand het weer aanzet."
                            if state.total_count == 1
                            else f"alle {state.total_count} componenten zijn uitgeschakeld en blijven uit "
                            "tot iemand ze weer aanzet."
                        )
                    ),
                    expects_no_application_pods=True,
                    badge="Uitgeschakeld",
                    details={"disabled": state.disabled_count, "total": state.total_count},
                )
            ]
        if state.is_partially_disabled:
            return [
                DeploymentStateFact(
                    service=self.service_type.value,
                    summary=(
                        f"{state.disabled_count} van de {state.total_count} componenten van deze "
                        + ("deployment is" if state.disabled_count == 1 else "deployment zijn")
                        + " uitgeschakeld; de rest draait gewoon."
                    ),
                    expects_no_application_pods=False,
                    # No ``expects_no_application_pods``, so this word stands NEXT to the
                    # health verdict instead of taking its place: the rest of the
                    # deployment is still supposed to serve traffic and its health is
                    # still the thing to report.
                    badge=f"{state.disabled_count} van {state.total_count} componenten uitgeschakeld",
                    details={"disabled": state.disabled_count, "total": state.total_count},
                )
            ]
        return []

    @on(ActionEvent.REDEPLOY)
    async def reenable_on_rollout(self, ctx: RedeployContext) -> list[str]:
        """Switch a component back on when new content is rolled out onto it (RC-37).

        Every automatic disable is a judgement about the content that was running:
        ``ImagePullBackOff`` about an image that could not be fetched, ``OOMKilled`` about
        one that ran out of memory, a crash loop about one that would not stay up. A
        rollout replaces exactly that content, so the judgement is about something that is
        no longer there and the component goes back on -- whatever the reason said.

        This is deliberately unconditional, and the alternative was really considered: an
        OOM will probably come back, so only lifting an image-pull disable would avoid one
        cycle of off-on-off. It is the wrong trade. A new image is often precisely the
        memory-leak fix, nothing else ever clears an OOM disable (the re-enable sweep keys
        on ``disabled-image``, which only image-pull disables carry), and a component that
        is off forever with no path back is a worse failure than one that goes off again
        five minutes later -- with the reason then pointing at the image that actually
        caused it. It also cannot flap on its own: the only thing that lifts a disable
        here is a person rolling something out.

        Every disable is cleared, wherever it sits. An earlier version spared a
        ``disabled`` flag on the component *definition*, on the reading that it was a
        deliberate project-wide decision. Measured on 6 August, that reading is wrong:
        ``set_component_disabled`` (the definition-level setter) has NO callers, there is
        no editable and no route for it, so a user cannot make that decision anywhere in
        OPI. Sparing it protected a case that does not occur, while a value that did end
        up in a file by hand would have kept the component off forever -- new image and
        all. Rolling out new content says plainly enough that it is meant to run.
        """
        from opi.handlers.project_file_handler import ProjectFileHandler

        handler = ProjectFileHandler()
        notices: list[str] = []
        for component in ctx.deployment.get("components", []) or []:
            reference = component.get("reference", "")
            if reference not in ctx.component_names:
                continue
            is_disabled, reason_text = handler.extract_deployment_component_disabled(
                ctx.project_data, ctx.deployment_name, reference
            )
            if not is_disabled:
                continue
            reason = str(reason_text or "")
            handler.set_deployment_component_disabled(ctx.project_data, ctx.deployment_name, reference, False, "")
            notices.append(
                f"Component '{reference}' stond uitgeschakeld"
                + (f" ({reason})" if reason else "")
                + " en is weer aangezet, want er is nieuwe inhoud uitgerold."
            )
        return notices

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
