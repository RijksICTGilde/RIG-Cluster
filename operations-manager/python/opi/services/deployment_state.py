"""Collect what the services know about one deployment (RC-28).

A deployment can be in a situation a service put it in -- sleep-mode scales the
application to zero and parks a waker in front of it -- and until now nothing outside
that service knew. Generic code then had to infer the situation from the cluster, which
is how a waker's ``ImagePullBackOff`` got reported as the component's own failure.

This module asks instead: it scans ``HookPoint.DEPLOYMENT_STATE`` and returns the facts
the services report, so no caller names a service. The result is deliberately a set of
facts plus one narrow consequence (``expects_no_application_pods``), never a health
verdict -- see ``DeploymentStateFact``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opi.services.catalog.base import DeploymentStateContext, DeploymentStateFact
from opi.services.registry import services_for_hook
from opi.services.services_enums import HookPoint


@dataclass
class DeploymentState:
    """The facts the services report about one deployment."""

    facts: list[DeploymentStateFact] = field(default_factory=list)

    @property
    def expects_no_application_pods(self) -> bool:
        """Whether a service says the application's own pods are meant to be absent.

        The single operational consequence generic code may draw from these facts. It
        says nothing about pods that ARE running: a problem observed on a real
        application pod stays a problem, whatever a service reports here.
        """
        return any(fact.expects_no_application_pods for fact in self.facts)

    @property
    def summaries(self) -> list[str]:
        return [fact.summary for fact in self.facts]

    @property
    def replacing_badges(self) -> list[str]:
        """The words that take the place of the green "Healthy" (RC-35).

        A fact that says the application's own pods are meant to be absent is a fact
        about a deployment where zero replicas is the intent -- and zero replicas is
        exactly what ArgoCD calls Healthy. So its badge replaces that one verdict; every
        other verdict is really observed and keeps its badge.

        Two services can report at once (a deployment that is asleep AND has all its
        components switched off), and then BOTH words are shown rather than one of them
        winning: "slaapstand" and "uitgeschakeld" ask different things of the reader, and
        dropping either would leave a user unable to tell whether to act. Sorted by
        service so the card never depends on the order the registry happens to have.
        """
        return self._badges(expects_no_application_pods=True)

    @property
    def accompanying_badges(self) -> list[str]:
        """The words that stand NEXT to the health verdict (RC-35).

        Part of the deployment is still supposed to serve traffic -- a partly switched-off
        deployment is the case -- so its health is still the thing to report and this only
        says what else is true about it.
        """
        return self._badges(expects_no_application_pods=False)

    def _badges(self, *, expects_no_application_pods: bool) -> list[str]:
        return [
            fact.badge
            for fact in sorted(self.facts, key=lambda fact: fact.service)
            if fact.badge and fact.expects_no_application_pods is expects_no_application_pods
        ]


def collect_deployment_state(project_data: dict[str, Any], deployment_name: str) -> DeploymentState:
    """What the project's services know about ``deployment_name``.

    Reads the project file only (no cluster calls), so this is cheap enough for a page
    render and safe to call before interpreting an observation. An unknown deployment
    name yields an empty state rather than an error: a caller asking about a deployment
    that is not in the file has no state to weigh, which is exactly what it gets.
    """
    deployment = next((d for d in project_data.get("deployments", []) or [] if d.get("name") == deployment_name), None)
    if deployment is None:
        return DeploymentState()

    ctx = DeploymentStateContext(
        project_name=project_data.get("name", ""),
        project_data=project_data,
        deployment=deployment,
    )

    # Deliberately NOT filtered through ``applies_to``: a service records what it did in
    # the project file, and that record outranks whether the project happens to list the
    # service today. Sleep-mode is the case in point -- it can be switched on for a whole
    # cluster without a project selecting it, so a selection filter would drop the state
    # of exactly the deployments that are asleep. A service that did nothing reports
    # nothing, so asking everyone costs nothing.
    facts: list[DeploymentStateFact] = []
    for service in services_for_hook(HookPoint.DEPLOYMENT_STATE):
        facts.extend(service.deployment_state(ctx))
    return DeploymentState(facts=facts)
