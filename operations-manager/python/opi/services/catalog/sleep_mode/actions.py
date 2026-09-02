"""The sleep-mode deployment actions: the wake/sleep toggle in the UI.

Bound onto the service's ServiceDefinition (see ``__init__.py``) so the generic
deployment-actions template collects it like any other service's buttons, instead of the
template deriving the condition itself. Exactly one button is shown, depending on the
deployment's sleep state: "Deployment slapen" while awake, "Applicatie wekken" otherwise.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.sleep_mode import config as sleep_config
from opi.services.catalog.sleep_mode.config import SleepModeConfigError
from opi.services.catalog.sleep_mode.state import STATE_AWAKE, read
from opi.services.services import DeploymentAction


def _find_deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
    for deployment in project_data.get("deployments", []) or []:
        if deployment.get("name") == deployment_name:
            return deployment
    return None


def sleep_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    """The wake/sleep toggle for a deployment, or no button when sleep-mode is off.

    Shown whenever sleep-mode is enabled for this project/cluster. Deliberately NOT
    scoped by ``match``: that selects which deployments the sweeper puts to sleep on a
    deadline, and this button is the manual half. Gating both on it meant that switching
    the service on and leaving ``match`` empty produced no button anywhere, on any
    deployment, with nothing to say why -- and ``match`` is empty by default.

    Nothing in the mechanism needs the scope. Sleeping is carried out from the stored
    state: replicas go to zero and the waker renders on ``sleep.state`` alone (see
    ``project_manager``). The sweeper stays out of it by itself -- ``decide_action`` only
    consults ``matches`` in the ``awake`` branch, so a deployment slept by hand outside
    the scope stays asleep until someone wakes it, which is what "manual" should mean.

    While awake, offer a manual sleep; while sleeping or waking, offer a wake. Both
    target the session-authenticated web routes (admin/owner), so a manual sleep and a
    manual wake are the two halves of one toggle.
    """
    deployment = _find_deployment(project_data, deployment_name)
    if deployment is None:
        return []
    try:
        config = sleep_config.load(project_data, deployment.get("cluster", ""))
    except SleepModeConfigError:
        # A broken sleep-mode config should not crash the details page; show no button.
        return []
    if config is None:
        return []

    project_name = project_data.get("name", "")
    base = f"/projects/{project_name}/deployments/{deployment_name}"
    if read(project_data, deployment_name).state == STATE_AWAKE:
        return [
            DeploymentAction(
                label="Deployment slapen",
                icon="klok",
                kind="secondary",
                endpoint=f"{base}/sleep",
                # Quote-free: rendered inline in a single-quoted JS string in the template.
                confirm_message=f"Deployment {deployment_name} handmatig in slaapstand zetten?",
                visible=True,
            )
        ]
    return [
        DeploymentAction(
            label="Applicatie wekken",
            # "uitvoering" staat NIET in de vertaaltabel van navigation_lotc.py, dus
            # to_nldd_icon() liet die naam ongewijzigd door, NLDD kent hem niet en er
            # verscheen niets - stil, want een onbekende naam levert geen fout op.
            # "klok" -> "timer" is het icoon dat deze dienst zelf al draagt en dat
            # aantoonbaar rendert; de slaapstand is ook waar deze actie over gaat.
            icon="klok",
            # Same weight as the other deployment actions (images bewerken, herverwerken):
            # waking is not more important than they are, it just happens less often.
            kind="secondary",
            endpoint=f"{base}/wake",
            confirm_message=f"Deployment {deployment_name} wekken uit de slaapstand?",
            visible=True,
        )
    ]
