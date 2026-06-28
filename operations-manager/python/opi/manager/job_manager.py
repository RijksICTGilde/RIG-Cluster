"""Manager for ad-hoc job runs: a Pod that runs an image + command once.

A job is a "run" (see run_support): a single Pod, applied and reaped by label,
wired with the deployment's database connection so it can run migrations. It has
no auth wall / service / ingress; monitoring is the pod phase plus a live log
tail (the existing component log viewer, since the pod carries an `app=` label).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import get_namespace_prefix
from opi.core.config import settings
from opi.generation.manifests import render_template
from opi.manager.run_support import (
    ANNOT_EXPIRES,
    ANNOT_OPENED_BY,
    LABEL_RUN,
    LABEL_RUN_DEPLOYMENT,
    LABEL_RUN_KIND,
    LABEL_RUN_PROJECT,
    apply_bundle,
    delete_bundle,
    find_deployment,
    parse_expires,
    resolve_image,
)
from opi.services.project_service import get_project_service
from opi.services.runs_service import RunKind, RunStatus, get_runs_service
from opi.utils.naming import generate_job_name
from opi.utils.secrets import DatabaseSecret

logger = logging.getLogger(__name__)

# Job-specific annotations (image/command for display + history).
ANNOT_IMAGE = "rig.zad/job-image"
ANNOT_COMMAND = "rig.zad/job-command"

# Pod phase -> run/UI state.
_PHASE_TO_STATE = {
    "Pending": "starting",
    "Running": "running",
    "Succeeded": "succeeded",
    "Failed": "failed",
}


class JobError(Exception):
    """Raised when a job run cannot be started or torn down."""


@dataclass
class JobRun:
    """Public description of a job run (live state from the pod)."""

    session_id: str
    name: str
    namespace: str
    project: str
    deployment: str
    image: str
    command: str
    opened_by: str
    expires_at: datetime
    state: str  # none|starting|running|succeeded|failed


class JobManager:
    """Orchestrates the lifecycle of ad-hoc job pods."""

    def __init__(self) -> None:
        self._kubectl = create_kubectl_connector()
        self._project_service = get_project_service()

    def _job_selector(self, deployment_name: str) -> str:
        return f"{LABEL_RUN_DEPLOYMENT}={deployment_name},{LABEL_RUN_KIND}={RunKind.JOB}"

    async def start(self, project_name: str, deployment_name: str, image: str, command: str, started_by: str) -> JobRun:
        """Provision and apply a job pod; return its description (state=starting)."""
        if not image.strip():
            raise JobError("Geen image opgegeven")
        # Shared local/ image handling (Kind-loaded images -> pull policy Never).
        image, image_pull_policy = resolve_image(image)
        # Command is optional: empty means run the image's default entrypoint/cmd.
        command = command.strip()

        project = self._project_service.get_project(project_name)
        if not project or not project.data:
            raise JobError(f"Project '{project_name}' niet gevonden")
        deployment = find_deployment(project.data, deployment_name)
        if not deployment:
            raise JobError(f"Deployment '{deployment_name}' niet gevonden in project '{project_name}'")
        cluster = deployment.get("cluster", settings.CLUSTER_MANAGER)
        if cluster != settings.CLUSTER_MANAGER:
            raise JobError(
                f"Deployment '{deployment_name}' draait op cluster '{cluster}', niet beheerd door deze instance."
            )

        namespace = f"{get_namespace_prefix(cluster)}{project_name}"

        # One job per deployment at a time (stop the previous one first).
        existing = await self._kubectl.get_resources_by_label("pod", namespace, self._job_selector(deployment_name))
        if existing:
            raise JobError("Er draait al een job voor deze deployment. Stop die eerst.")

        # Wire the deployment's database connection if it has one (the migrations use case).
        db_secret = await DatabaseSecret.get_data(
            kubectl_connector=self._kubectl, namespace=namespace, prefix=deployment_name
        )
        db_secret_name = DatabaseSecret.get_secret_name(deployment_name) if db_secret else None

        session_id = secrets.token_hex(4)
        name = generate_job_name(project_name, deployment_name, session_id)
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.JOB_TTL_SECONDS)

        extra_labels = {
            LABEL_RUN: session_id,
            LABEL_RUN_KIND: str(RunKind.JOB),
            LABEL_RUN_PROJECT: project_name,
            LABEL_RUN_DEPLOYMENT: deployment_name,
        }
        extra_annotations = {
            ANNOT_EXPIRES: expires_at.isoformat(),
            ANNOT_OPENED_BY: started_by,
            ANNOT_IMAGE: image,
            ANNOT_COMMAND: command,
        }

        common = {
            "name": name,
            "namespace": namespace,
            "project": {"name": project_name},
            "cluster": cluster,
            "extra_labels": extra_labels,
            "extra_annotations": extra_annotations,
        }
        pod = render_template(
            "job-pod.yaml.jinja",
            {
                **common,
                "target_deployment": deployment_name,
                "ttl_seconds": settings.JOB_TTL_SECONDS,
                "image": image,
                "image_pull_policy": image_pull_policy,
                "command": command,
                "db_secret_name": db_secret_name,
            },
        )
        ok, stderr = await apply_bundle(self._kubectl, namespace, [pod], cluster)
        if not ok:
            raise JobError(f"Kon de job niet starten: {stderr.strip() or 'onbekende fout'}")

        try:
            await get_runs_service().create_run(
                kind=RunKind.JOB,
                session_id=session_id,
                cluster=cluster,
                project=project_name,
                deployment=deployment_name,
                namespace=namespace,
                name=name,
                spec={"image": image, "command": command},
                url=None,
                started_by=started_by,
                expires_at=expires_at,
            )
        except Exception:
            logger.exception("Failed to record run for job session %s (continuing)", session_id)

        logger.info("Started job '%s' for %s/%s by %s", name, project_name, deployment_name, started_by)
        return JobRun(
            session_id=session_id,
            name=name,
            namespace=namespace,
            project=project_name,
            deployment=deployment_name,
            image=image,
            command=command,
            opened_by=started_by,
            expires_at=expires_at,
            state="starting",
        )

    async def get_job(self, project_name: str, deployment_name: str) -> JobRun | None:
        """Return the live job for a deployment (None if none), reconciling its run row."""
        cluster = settings.CLUSTER_MANAGER
        namespace = f"{get_namespace_prefix(cluster)}{project_name}"
        pods = await self._kubectl.get_resources_by_label("pod", namespace, self._job_selector(deployment_name))
        if not pods:
            return None
        run = self._job_from_pod(pods[0], namespace, project_name)

        # Keep the registry row in step with the pod (best-effort).
        try:
            if run.state == "running":
                await get_runs_service().mark_running(run.session_id)
            elif run.state == "succeeded":
                await get_runs_service().mark_ended(run.session_id, RunStatus.SUCCEEDED)
            elif run.state == "failed":
                await get_runs_service().mark_ended(run.session_id, RunStatus.FAILED)
        except Exception:
            logger.exception("Failed to reconcile run row for job session %s", run.session_id)
        return run

    @staticmethod
    def _job_from_pod(pod: dict[str, Any], namespace: str, project_name: str) -> JobRun:
        meta = pod.get("metadata", {})
        labels = meta.get("labels", {}) or {}
        annotations = meta.get("annotations", {}) or {}
        phase = (pod.get("status", {}) or {}).get("phase", "")
        expires_at = parse_expires(annotations.get(ANNOT_EXPIRES)) or datetime.now(UTC)
        return JobRun(
            session_id=labels.get(LABEL_RUN, ""),
            name=meta.get("name", ""),
            namespace=namespace,
            project=project_name,
            deployment=labels.get(LABEL_RUN_DEPLOYMENT, ""),
            image=annotations.get(ANNOT_IMAGE, ""),
            command=annotations.get(ANNOT_COMMAND, ""),
            opened_by=annotations.get(ANNOT_OPENED_BY, ""),
            expires_at=expires_at,
            state=_PHASE_TO_STATE.get(phase, "starting"),
        )

    async def teardown_session(self, project_name: str, session_id: str, ended_by: str | None = None) -> str | None:
        """Stop/remove a job by session id. Returns the deployment name, or None."""
        cluster = settings.CLUSTER_MANAGER
        namespace = f"{get_namespace_prefix(cluster)}{project_name}"
        pods = await self._kubectl.get_resources_by_label("pod", namespace, f"{LABEL_RUN}={session_id}")
        deployment = None
        if pods:
            deployment = (pods[0].get("metadata", {}).get("labels", {}) or {}).get(LABEL_RUN_DEPLOYMENT)
        await delete_bundle(self._kubectl, namespace, session_id)
        try:
            await get_runs_service().mark_ended(session_id, RunStatus.STOPPED, ended_by=ended_by)
        except Exception:
            logger.exception("Failed to mark run ended for job session %s", session_id)
        return deployment


_job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    """Return the process-wide JobManager."""
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager
