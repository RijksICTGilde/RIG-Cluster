"""Background task for running one-off Kubernetes Jobs.

Uses the ProjectManager flow to ensure all services are provisioned and
ArgoCD-synced before running the job. This guarantees that secrets for
databases, MinIO, Keycloak, Redis, etc. exist in the namespace.
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
    """Run a one-off Kubernetes Job as a background task with progress tracking.

    Flow:
        1. Run the ProjectManager flow for this deployment (idempotent).
           This ensures namespace, databases, secrets, and ArgoCD sync.
        2. Resolve the namespace from project data.
        3. Create and run the K8s Job via kubectl apply.
    """
    task_progress = TaskProgressManager(task_id, project_name)

    try:
        # Step 1: ensure deployment is fully provisioned via ProjectManager
        provision_task = task_progress.add_task("Deployment verwerken en synchroniseren")
        try:
            from opi.manager.project_manager import ProjectManager

            project_file_path = f"projects/{project_name}.yaml"
            project_manager = ProjectManager(project_file_relative_path=project_file_path)
            try:
                success = await project_manager.process_project_from_git(
                    project_file_path,
                    task_progress_manager=None,  # PM has its own progress; ours tracks the outer flow
                    deployment_name=deployment_name,
                )
            finally:
                await project_manager.close()

            if not success:
                task_progress.fail_task(provision_task, "Deployment verwerking mislukt")
                task_progress.fail_project("Deployment kon niet verwerkt worden")
                return
        except Exception as e:
            task_progress.fail_task(provision_task, str(e))
            task_progress.fail_project(str(e))
            return
        task_progress.complete_task(provision_task)

        # Step 2: resolve namespace and cluster
        resolve_task = task_progress.add_task("Namespace opzoeken")
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

        # Step 3: run the job
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
