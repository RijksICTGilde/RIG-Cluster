"""Generic redeploy runner: let every service clear the state it recorded (RC-37).

A deliberate rollout -- an image update, a deployment upsert -- replaces what a
deployment runs, and every state a service recorded about the previous content stops
holding at that moment. Before this hook, ``project_manager`` cleared exactly one such
state by name (``is_image_pull_disable_reason``), so a component disabled by the OOM
watcher stayed switched off after its image was fixed: the task succeeded, no deployment
appeared, and nothing said why.

This module scans ``HookPoint.REDEPLOY`` instead, so the rollout paths never name a
service and the next service that records state gets the moment for free. It only
mutates ``project_data`` in memory; the caller commits, because the caller is committing
the rollout itself and one commit for both is the point.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.catalog.base import RedeployContext
from opi.services.registry import services_for_hook
from opi.services.services_enums import HookPoint

logger = logging.getLogger(__name__)


def run_redeploy_hooks(
    project_name: str,
    project_data: dict[str, Any],
    deployment: dict[str, Any],
    component_names: list[str],
) -> list[str]:
    """Let every service clear its state for a deployment that is being rolled out.

    Args:
        project_name: The project.
        project_data: The in-memory project dict the caller is about to commit; hooks
            mutate it in place.
        deployment: The deployment dict WITHIN ``project_data`` -- not a copy, or the
            cleanups never reach the commit.
        component_names: References of the components this rollout puts new content on.

    Returns:
        One line per thing a service cleared, in the user's language, for the caller to
        surface. Empty when there was no state to clear.
    """
    # Deliberately NOT filtered through ``applies_to``, for the same reason
    # ``collect_deployment_state`` is not: a service records what it did in the project
    # file, and that record has to be cleaned up even if the project no longer lists the
    # service today. Sleep-mode is the case in point -- it can be switched on for a whole
    # cluster without a project selecting it. A service that recorded nothing returns
    # nothing, so asking everyone costs nothing.
    ctx = RedeployContext(
        project_name=project_name,
        project_data=project_data,
        deployment=deployment,
        component_names=component_names,
    )

    notices: list[str] = []
    for service in services_for_hook(HookPoint.REDEPLOY):
        notices.extend(service.on_redeploy(ctx))

    for notice in notices:
        logger.info(
            "redeploy: cleared state for %s/%s: %s",
            project_name,
            ctx.deployment_name,
            notice,
        )
    return notices
