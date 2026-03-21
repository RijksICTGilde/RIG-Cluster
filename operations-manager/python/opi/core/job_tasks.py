"""Background task for running one-off Kubernetes Jobs.

Follows the same pattern as backup_tasks.py: creates a TaskProgressManager,
resolves project/deployment info, and delegates to JobManager.
"""

from __future__ import annotations

import logging

from opi.core.task_manager import TaskProgressManager

logger = logging.getLogger(__name__)


async def run_job_task(
    task_id: str,
    project_name: str,
    deployment_name: str,
    image: str,
    command: str | None,
    env_vars: dict[str, str],
    env_from_secrets: list[str],
) -> None:
    """Run a one-off Kubernetes Job as a background task with progress tracking."""
    task_progress = TaskProgressManager(task_id, project_name)

    try:
        # Step 1: resolve namespace and cluster
        resolve_task = task_progress.add_task("Project en deployment opzoeken")
        try:
            from opi.core.backup_tasks import _resolve_deployment_info

            _project, _project_data, app_namespace, current_cluster = await _resolve_deployment_info(
                project_name, deployment_name
            )
        except Exception as e:
            task_progress.fail_task(resolve_task, str(e))
            task_progress.fail_project(str(e))
            return
        task_progress.complete_task(resolve_task)

        # Step 2: run the job
        from opi.connectors.kubectl import KubectlConnector
        from opi.manager.job_manager import JobManager

        kubectl = KubectlConnector()
        manager = JobManager(kubectl)

        result = await manager.run_job(
            namespace=app_namespace,
            project_name=project_name,
            deployment_name=deployment_name,
            image=image,
            command=command,
            env_vars=env_vars,
            env_from_secrets=env_from_secrets,
            cluster=current_cluster,
            progress=task_progress,
        )

        if result["status"] == "completed":
            task_progress.complete_project()
            logger.info(
                "Job task %s completed for %s/%s",
                task_id,
                project_name,
                deployment_name,
            )
        else:
            logs_snippet = (result.get("logs") or "")[:500]
            task_progress.fail_project(f"Job mislukt. Logs: {logs_snippet}")

    except Exception as e:
        logger.exception("Job task %s failed unexpectedly", task_id)
        task_progress.fail_project(f"Onverwachte fout: {e}")
