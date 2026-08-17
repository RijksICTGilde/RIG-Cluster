"""Sleeping and waking as a followable task, instead of a request that hangs.

Both transitions commit to git and then run the whole reprocessing pipeline, ArgoCD sync
included. Done inline, the browser sat on an open POST for tens of seconds with nothing
to show, which reads as a button that did nothing -- so this runs as an async task and
the page polls it, exactly like reprocessing a deployment already does.

The handler lives in the service package because the work is sleep-mode's; only the
registration is central, next to the other task types.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How each direction is named on the page, as a heading and as an outcome.
_STEPS = {
    "sleep": ("Deployment in slaapstand zetten", "Deployment slaapt"),
    "wake": ("Deployment wekken", "Deployment is gewekt"),
}


async def handle_sleep_transition(payload: dict[str, Any], progress: Any) -> dict[str, Any]:
    """Move one deployment to sleeping or awake, reporting progress while it runs.

    Args:
        payload: ``project_name``, ``deployment_name`` and ``direction`` ("sleep"/"wake").
        progress: PersistentTaskProgressManager for reporting progress.

    Returns:
        The new state and whether anything changed, in the shape the task result models
        expect. A no-op (already asleep, out of scope) completes rather than failing:
        the deployment is in the state the user asked for, which is not an error.
    """
    from opi.services.catalog.sleep_mode import flow

    project_name = payload["project_name"]
    deployment_name = payload["deployment_name"]
    direction = payload.get("direction", "sleep")
    running, done = _STEPS.get(direction, _STEPS["sleep"])

    # The flow itself names each step it takes (loading, committing) and the
    # reprocessing it triggers names the manifest and ArgoCD work, so the wait is
    # accounted for instead of sitting behind one bar.
    heading = progress.add_task(running, subject=deployment_name)
    try:
        result = (
            await flow.sleep(project_name, deployment_name, progress=progress)
            if direction == "sleep"
            else await flow.wake(project_name, deployment_name, progress=progress)
        )
    except Exception as exc:
        progress.fail_task(heading, str(exc))
        progress.fail_project(str(exc))
        raise
    progress.complete_task(heading)
    progress.update_current_step(done if result.changed else f"Geen wijziging nodig, deployment is al {result.state}")

    logger.info(
        "sleep-mode task %s for %s/%s: changed=%s state=%s",
        direction,
        project_name,
        deployment_name,
        result.changed,
        result.state,
    )
    return {
        "status": "success",
        "message": done if result.changed else f"Deployment was al {result.state}",
        "state": result.state,
        "changed": result.changed,
    }
