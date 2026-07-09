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


async def _monitor_argocd_and_deployment(
    _task_id: str, project_name: str, task_progress_manager: TaskProgressManager, monitor_task: str
) -> None:
    """Monitor the project's own ArgoCD applications until they are synced and healthy.

    The ``user-applications`` App-of-Apps creates individual ArgoCD
    Application resources for each project deployment (e.g.
    ``myproject-staging``).  Those project apps are what we care about -
    the parent ``user-applications`` syncs on its own schedule and may
    stay ``OutOfSync`` long after the project apps are ready.
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

        max_retries = 15  # Wait up to ~30 seconds for project apps
        argo_synced = False

        for attempt in range(max_retries):
            try:
                logger.debug(f"Checking project ArgoCD apps, attempt {attempt + 1}/{max_retries}")

                all_apps = await argo_connector.list_applications()
                project_apps = [
                    app for app in all_apps if app.get("metadata", {}).get("name", "").startswith(f"{project_name}-")
                ]

                if not project_apps:
                    logger.debug(f"No ArgoCD apps found yet for {project_name}")
                    await asyncio.sleep(2)
                    continue

                logger.debug(f"Found {len(project_apps)} app(s) for {project_name}")

                all_healthy = True
                for app in project_apps:
                    app_name = app.get("metadata", {}).get("name", "")
                    app_sync = app.get("status", {}).get("sync", {}).get("status", "Unknown")
                    app_health = app.get("status", {}).get("health", {}).get("status", "Unknown")
                    logger.debug(f"  {app_name} - Sync: {app_sync}, Health: {app_health}")

                    if not (app_sync == "Synced" and app_health in ["Healthy", "Progressing"]):
                        all_healthy = False
                        break

                if all_healthy:
                    argo_synced = True
                    app_names = [app.get("metadata", {}).get("name", "") for app in project_apps]
                    logger.info(f"All project ArgoCD apps healthy for {project_name}: {app_names}")
                    break

                await asyncio.sleep(2)

            except Exception as e:
                logger.warning(f"Error checking ArgoCD apps (attempt {attempt + 1}): {e}")
                await asyncio.sleep(2)

        if argo_synced:
            task_progress_manager.complete_task(argo_subtask)
        else:
            logger.warning(f"ArgoCD sync wait timed out for project apps: {project_name}")
            task_progress_manager.fail_task(
                argo_subtask,
                f"De wachttijd op ArgoCD is verstreken: niet alle apps van '{project_name}' waren binnen de "
                "wachttijd gesynct en gezond. Controleer de status van het project.",
            )

        task_progress_manager.complete_task(monitor_task)

    except Exception as e:
        logger.error(f"Error monitoring ArgoCD for {project_name}: {e}")
        task_progress_manager.fail_task(monitor_task, f"Monitoring failed: {e}")
