"""Job runner manager for executing one-off Kubernetes Jobs.

Handles the full lifecycle: render manifest, apply via kubectl, wait for
completion, collect logs, and clean up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

from opi.generation.manifests import render_template

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector
    from opi.core.task_manager import TaskProgressManager

logger = logging.getLogger(__name__)

# Container statuses that indicate unrecoverable errors
FATAL_CONTAINER_STATUSES = frozenset(
    {
        "imagepullbackoff",
        "errimagepull",
        "crashloopbackoff",
        "createcontainerconfigerror",
        "invalidimagename",
        "errimageneverpull",
    }
)


class JobManager:
    """Manages one-off Kubernetes Job execution."""

    def __init__(self, kubectl: KubectlConnector) -> None:
        self.kubectl = kubectl

    async def run_job(
        self,
        namespace: str,
        project_name: str,
        deployment_name: str,
        image: str,
        command: str | None,
        env_vars: dict[str, str],
        env_from_secrets: list[str],
        cluster: str,
        progress: TaskProgressManager,
    ) -> dict[str, str]:
        """Run a job and return result with logs.

        Returns:
            Dict with keys: status ("completed" | "failed"), logs (str)
        """
        job_name = _generate_job_name(project_name, deployment_name)

        # Task: create and apply job
        apply_task = progress.add_task("Job aanmaken")
        try:
            await self._render_and_apply_job(
                namespace=namespace,
                job_name=job_name,
                project_name=project_name,
                deployment_name=deployment_name,
                image=image,
                command=command,
                env_vars=env_vars,
                env_from_secrets=env_from_secrets,
                cluster=cluster,
            )
        except Exception as e:
            progress.fail_task(apply_task, str(e))
            raise
        progress.complete_task(apply_task)

        # Task: wait for job completion
        wait_task = progress.add_task("Wachten op voltooiing")
        try:
            success = await self._wait_for_job(namespace, job_name, timeout=3600, progress=progress)
            logs = await self._get_job_pod_logs(namespace, job_name)

            if not success:
                progress.fail_task(wait_task, "Job mislukt")
                return {"status": "failed", "logs": logs}

            progress.complete_task(wait_task)
            return {"status": "completed", "logs": logs}
        finally:
            await self._cleanup_job(namespace, job_name)

    async def _render_and_apply_job(
        self,
        namespace: str,
        job_name: str,
        project_name: str,
        deployment_name: str,
        image: str,
        command: str | None,
        env_vars: dict[str, str],
        env_from_secrets: list[str],
        cluster: str,
    ) -> None:
        """Render Job manifest from template and apply via kubectl."""
        variables = {
            "job_name": job_name,
            "namespace": namespace,
            "project_name": project_name,
            "deployment_name": deployment_name,
            "image": image,
            "command": command,
            "env_vars": env_vars,
            "env_from_secrets": env_from_secrets,
            "cluster": cluster,
        }

        manifest_yaml = render_template("job.yaml.jinja", variables)

        stdout, stderr, code = await self.kubectl.run_command(
            ["apply", "-f", "-"],
            stdin_input=manifest_yaml,
        )
        if code != 0:
            msg = f"kubectl apply mislukt: {stderr}"
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info("Job %s applied in namespace %s", job_name, namespace)

    async def _wait_for_job(
        self,
        namespace: str,
        job_name: str,
        timeout: int,
        progress: TaskProgressManager,
    ) -> bool:
        """Poll job status until complete/failed/timeout."""
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.error("Timeout waiting for job %s after %ds", job_name, timeout)
                return False

            stdout, stderr, code = await self.kubectl.run_command(
                ["get", "job", job_name, "-n", namespace, "-o", "json"]
            )

            if code != 0:
                logger.warning("Failed to get job status: %s", stderr)
                await asyncio.sleep(5)
                continue

            try:
                job_data = json.loads(stdout)
            except json.JSONDecodeError:
                await asyncio.sleep(5)
                continue

            # Check job conditions
            conditions = job_data.get("status", {}).get("conditions", [])
            for condition in conditions:
                if condition.get("type") == "Complete" and condition.get("status") == "True":
                    logger.info("Job %s completed successfully", job_name)
                    return True
                if condition.get("type") == "Failed" and condition.get("status") == "True":
                    reason = condition.get("reason", "Unknown")
                    logger.error("Job %s failed: %s", job_name, reason)
                    return False

            # Check pod statuses for fatal errors (ImagePullBackOff etc.)
            pod_failed = await self._check_job_pod_fatal_errors(namespace, job_name)
            if pod_failed:
                return False

            await asyncio.sleep(5)

    async def _check_job_pod_fatal_errors(self, namespace: str, job_name: str) -> bool:
        """Check if the job's pod has a fatal container error. Returns True if fatal."""
        stdout, stderr, code = await self.kubectl.run_command(
            ["get", "pods", "-n", namespace, "-l", f"job-name={job_name}", "-o", "json"]
        )
        if code != 0:
            return False

        try:
            pods_data = json.loads(stdout)
        except json.JSONDecodeError:
            return False

        for pod in pods_data.get("items", []):
            container_statuses = pod.get("status", {}).get("containerStatuses", [])
            for cs in container_statuses:
                waiting = cs.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "").lower()
                if reason in FATAL_CONTAINER_STATUSES:
                    message = waiting.get("message", "No details")
                    logger.error("Job pod has fatal error: %s - %s", reason, message)
                    return True

        return False

    async def _get_job_pod_logs(self, namespace: str, job_name: str) -> str:
        """Get logs from the job's pod."""
        stdout, stderr, code = await self.kubectl.run_command(
            ["logs", "-n", namespace, "-l", f"job-name={job_name}", "--tail=200"]
        )
        if code != 0:
            return f"Kan logs niet ophalen: {stderr}"
        return stdout

    async def _cleanup_job(self, namespace: str, job_name: str) -> None:
        """Delete the job (cascades to pods). Best effort."""
        try:
            await self.kubectl.run_command(["delete", "job", job_name, "-n", namespace, "--ignore-not-found=true"])
            logger.debug("Deleted job %s", job_name)
        except Exception as e:
            logger.warning("Failed to delete job %s: %s", job_name, e)


def _generate_job_name(project_name: str, deployment_name: str) -> str:
    """Generate a unique job name, truncated to 63 chars (K8s limit)."""
    suffix = uuid.uuid4().hex[:8]
    base = f"job-{project_name}-{deployment_name}"
    # 63 - 1 (dash) - 8 (suffix) = 54 max base length
    base = base[:54]
    return f"{base}-{suffix}"
