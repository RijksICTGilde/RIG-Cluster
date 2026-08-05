"""Whether a deployment runs, runs partly, or is switched off entirely (RC-31).

A component with ``disabled: true`` is rendered with ``replicas: 0``. ArgoCD calls zero
replicas healthy -- nothing is failing -- and that verdict used to travel unfiltered to
the dashboard banner, the deployment card and the V2 API. The result was a page that said
"uitgeschakeld" and "Healthy" on the same line.

This module answers the one question those three places need: how much of a deployment is
switched off. It reads the PROJECT FILE only, never the cluster, for the same reason
RC-28's deployment-state hook does: zero replicas in the cluster can also mean something
went wrong, so the intent has to come from where the intent is recorded.

Three states, not two: a deployment with one of four components off is a different
situation than one that is off entirely, and collapsing them loses the difference that
tells a user whether anything is still serving traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DisabledState(StrEnum):
    """How much of a deployment is switched off."""

    RUNNING = "running"
    PARTIALLY_DISABLED = "partially-disabled"
    DISABLED = "disabled"


@dataclass(frozen=True)
class DeploymentDisabledState:
    """The switched-off state of one deployment, with the counts behind it.

    The counts are part of the answer, not a detail: "2 van de 4 componenten
    uitgeschakeld" is what a partially disabled deployment has to say, and deriving that
    again at each of the three call sites is how they drift apart.
    """

    state: DisabledState
    disabled_count: int
    total_count: int

    @property
    def is_disabled(self) -> bool:
        """The whole deployment is off: nothing of it is meant to be running."""
        return self.state is DisabledState.DISABLED

    @property
    def is_partially_disabled(self) -> bool:
        return self.state is DisabledState.PARTIALLY_DISABLED


def component_is_disabled(project_data: dict[str, Any], component: dict[str, Any]) -> bool:
    """Whether one deployment-component is switched off.

    Same precedence as ``ProjectFileHandler.extract_deployment_component_disabled``: an
    inline flag on the deployment-component wins, otherwise the component definition
    decides. Resolved here with plain dict access rather than through the handler,
    because this runs for every deployment of every project on a dashboard render.
    """
    if "disabled" in component:
        return bool(component.get("disabled"))

    reference = component.get("reference")
    for definition in project_data.get("components", []) or []:
        if definition.get("name") == reference:
            return bool(definition.get("disabled"))
    return False


def deployment_disabled_state(project_data: dict[str, Any], deployment_name: str) -> DeploymentDisabledState:
    """How much of ``deployment_name`` is switched off.

    A deployment without components is ``RUNNING`` with zero counts: there is nothing
    switched off, and calling it "uitgeschakeld" would be a second untruth in place of
    the first. An unknown deployment name yields the same empty answer -- a caller asking
    about a deployment that is not in the file has nothing to weigh.
    """
    deployment = next(
        (d for d in project_data.get("deployments", []) or [] if d.get("name") == deployment_name),
        None,
    )
    components = (deployment or {}).get("components", []) or []
    total = len(components)
    disabled = sum(1 for component in components if component_is_disabled(project_data, component))

    if total == 0 or disabled == 0:
        state = DisabledState.RUNNING
    elif disabled == total:
        state = DisabledState.DISABLED
    else:
        state = DisabledState.PARTIALLY_DISABLED
    return DeploymentDisabledState(state=state, disabled_count=disabled, total_count=total)
