"""
Simple background task processor using the new TaskProgressManager.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.core.task_manager import TaskProgressManager

logger = logging.getLogger(__name__)


def _app_status(app: dict) -> tuple[str, str, str]:
    """(name, sync status, health status) for one ArgoCD Application."""
    status = app.get("status", {})
    return (
        app.get("metadata", {}).get("name", ""),
        status.get("sync", {}).get("status", "Unknown"),
        status.get("health", {}).get("status", "Unknown"),
    )


async def _monitor_argocd_and_deployment(
    _task_id: str,
    project_name: str,
    task_progress_manager: TaskProgressManager,
    monitor_task: str,
    deployment_names: list[str] | None = None,
) -> None:
    """Monitor the project's own ArgoCD applications until they are synced and healthy.

    The ``user-applications`` App-of-Apps creates individual ArgoCD
    Application resources for each project deployment (e.g.
    ``myproject-staging``).  Those project apps are what we care about -
    the parent ``user-applications`` syncs on its own schedule and may
    stay ``OutOfSync`` long after the project apps are ready.

    ``deployment_names`` scopes the wait to the deployments this task actually
    touched. Without it the wait covers every app of the project, so one
    permanently Degraded app -- typically a PR environment whose own pods do not
    start -- makes every later deploy of that project time out, forever, no
    matter how well the deploy itself went.
    """
    try:
        logger.info(f"Starting ArgoCD monitoring for project: {project_name}")

        from opi.connectors.argo import create_argo_connector

        argo_connector = create_argo_connector()

        # Give ArgoCD time to pick up the git changes and create the project apps
        await asyncio.sleep(5)

        argo_subtask = task_progress_manager.add_subtask(monitor_task, "Wachten op ArgoCD sync voltooiing")

        # Allow the triggered syncs to propagate
        await asyncio.sleep(8)

        wanted_apps = {f"{project_name}-{name}" for name in deployment_names} if deployment_names else None
        max_retries = 15  # Wait up to ~30 seconds for project apps
        argo_synced = False
        last_seen: list[tuple[str, str, str]] = []

        for attempt in range(max_retries):
            try:
                logger.debug(f"Checking project ArgoCD apps, attempt {attempt + 1}/{max_retries}")

                all_apps = await argo_connector.list_applications()
                project_apps = [
                    app for app in all_apps if app.get("metadata", {}).get("name", "").startswith(f"{project_name}-")
                ]
                if wanted_apps is not None:
                    project_apps = [
                        app for app in project_apps if app.get("metadata", {}).get("name", "") in wanted_apps
                    ]

                if not project_apps:
                    logger.debug(f"No ArgoCD apps found yet for {project_name}")
                    await asyncio.sleep(2)
                    continue

                # Full snapshot, not a break on the first offender: on timeout the
                # message has to name what was blocking, and "which app and in what
                # state" is the question that was previously unanswerable without
                # turning on debug logging in production.
                last_seen = [_app_status(app) for app in project_apps]
                logger.debug(
                    "ArgoCD apps for %s: %s",
                    project_name,
                    ", ".join(f"{n} sync={s} health={h}" for n, s, h in last_seen),
                )

                if all(s == "Synced" and h in ("Healthy", "Progressing") for _, s, h in last_seen):
                    argo_synced = True
                    logger.info(f"All project ArgoCD apps healthy for {project_name}: {[n for n, _, _ in last_seen]}")
                    break

                await asyncio.sleep(2)

            except Exception as e:
                logger.warning(f"Error checking ArgoCD apps (attempt {attempt + 1}): {e}")
                await asyncio.sleep(2)

        if argo_synced:
            task_progress_manager.complete_task(argo_subtask)
        else:
            # Neither outcome below is a failure of this deploy, so neither is logged
            # as an error: the change was committed, pushed and picked up by ArgoCD
            # before we ever got here. Failing the step contradicted our own wizard
            # text ("een time-out betekent niet dat het aanmaken is mislukt") and fed
            # the log watcher, which turned each one into a push notification.
            blocking = [(n, s, h) for n, s, h in last_seen if not (s == "Synced" and h in ("Healthy", "Progressing"))]
            detail = ", ".join(f"{n} (sync={s}, health={h})" for n, s, h in blocking) or "geen app-status ontvangen"
            degraded = [n for n, _, h in blocking if h in ("Degraded", "Missing")]

            if degraded:
                # The app's own pods are failing (bad image, crash loop). Same class as
                # the pod-health check elsewhere: "app crashing, not a deploy failure".
                logger.warning(
                    "ArgoCD wait for %s ended with unhealthy app(s), which is the application's own "
                    "problem and not a deploy failure: %s",
                    project_name,
                    detail,
                )
                task_progress_manager.complete_task(argo_subtask)
                notice = task_progress_manager.add_subtask(
                    monitor_task,
                    f"Let op: de deployment is uitgerold, maar draait niet gezond: {detail}. "
                    "Dat ligt aan de applicatie zelf (bijvoorbeeld een image dat niet gepulld kan "
                    "worden of een pod die crasht), niet aan het uitrollen.",
                )
                task_progress_manager.complete_task(notice)
            else:
                logger.warning(
                    "ArgoCD had not finished syncing %s within the wait window; still: %s", project_name, detail
                )
                task_progress_manager.complete_task(argo_subtask)
                notice = task_progress_manager.add_subtask(
                    monitor_task,
                    f"ArgoCD was nog niet klaar met synchroniseren binnen de wachttijd: {detail}. "
                    "De wijziging is wel gepubliceerd; ArgoCD rondt dit zelf af.",
                )
                task_progress_manager.complete_task(notice)

        task_progress_manager.complete_task(monitor_task)

    except Exception as e:
        logger.error(f"Error monitoring ArgoCD for {project_name}: {e}")
        task_progress_manager.fail_task(monitor_task, f"Monitoring failed: {e}")
