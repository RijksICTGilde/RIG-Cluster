"""Orchestration for wake/status: the one path that mutates sleep state via git.

Both the API route (waker pod, wake-token auth) and the web route (UI button, session
auth) call ``wake`` here, so the load -> transition -> commit -> reprocess sequence lives
in exactly one place. ``wake`` uses the mutation-path load (fresh, migrated data from
git, not the cache) so a stale cache cannot overwrite a concurrent change; ``status`` is
read-only and hot (polled every few seconds), so it reads the cached project data and
only hits kubectl.

Heavy imports (ProjectManager, connectors) are done inside the functions so importing
this module -- or the catalog -- stays light.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class DeploymentNotFound(Exception):
    """The project or deployment does not exist."""


class InvalidWakeToken(Exception):
    """A wake token was missing or did not match the deployment's stored token."""


@dataclass
class WakeResult:
    """Outcome of a wake attempt: whether a transition happened, and the new state."""

    changed: bool
    state: str


class _Steps:
    """Names the steps of a transition on a watching task, or does nothing.

    A step is only opened when the work behind it is about to run, and a step that
    is skipped is never completed: a no-op transition says so in its own words
    instead of showing a row of green checks for work that never happened.
    """

    def __init__(self, progress: Any | None):
        self._progress = progress
        self._current: str | None = None

    def begin(self, name: str) -> None:
        """Close the previous step as done and open a new one."""
        self.done()
        if self._progress is not None:
            self._current = self._progress.add_task(name)

    def done(self) -> None:
        """Complete the open step, if any."""
        if self._progress is not None and self._current is not None:
            self._progress.complete_task(self._current)
        self._current = None

    def failed(self, error: str) -> None:
        """Fail the open step, if any."""
        if self._progress is not None and self._current is not None:
            self._progress.fail_task(self._current, error)
        self._current = None

    def note(self, name: str) -> None:
        """Record a completed statement of fact (e.g. 'nothing to do')."""
        self.done()
        if self._progress is not None:
            self._progress.complete_task(self._progress.add_task(name))


def _find_deployment(project_data: dict, deployment_name: str) -> dict | None:
    for deployment in project_data.get("deployments", []) or []:
        if deployment.get("name") == deployment_name:
            return deployment
    return None


async def _verify_token(project_data: dict, deployment_name: str, presented_token: str) -> None:
    from opi.services.catalog.sleep_mode import token as token_mod
    from opi.services.catalog.sleep_mode.state import read

    stored = read(project_data, deployment_name).wake_token
    if not stored:
        raise InvalidWakeToken(f"No wake token set for deployment '{deployment_name}'")
    plain = await token_mod.decrypt(stored, project_data)
    if not token_mod.compare(presented_token, plain):
        raise InvalidWakeToken("Wake token mismatch")


async def wake(
    project_name: str,
    deployment_name: str,
    *,
    presented_token: str | None = None,
    now: datetime | None = None,
    progress: Any | None = None,
) -> WakeResult:
    """Move a matching, sleeping deployment to ``waking`` and reprocess it.

    Idempotent: only ``sleeping -> waking`` applies; anything else (already waking/awake,
    a non-matching deployment) is a no-op with ``changed=False`` and no commit. When
    ``presented_token`` is given (the waker pod), it is verified against the stored token;
    the web route passes ``None`` because it is session-authenticated.

    ``progress`` is the task progress manager when a user is watching this run; it names
    the steps as they happen. The waker pod passes nothing and nothing is reported.
    """
    from opi.core.config import settings
    from opi.manager.project_manager import ProjectManager
    from opi.services.catalog.sleep_mode import config as sleep_config
    from opi.services.catalog.sleep_mode import service
    from opi.services.catalog.sleep_mode import state as sleep_state
    from opi.services.project_store import get_project_store
    from opi.services.resource_tuning_service import trigger_reprocessing

    now = now or datetime.now(UTC)
    steps = _Steps(progress)
    project = get_project_store().get(project_name)
    if not project:
        raise DeploymentNotFound(f"Project '{project_name}' not found")
    filename = project.filename

    project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")
    try:
        steps.begin("Projectgegevens ophalen")
        project_data = await project_manager.get_contents()
        deployment = _find_deployment(project_data, deployment_name)
        if deployment is None:
            steps.failed(f"Deployment '{deployment_name}' bestaat niet in dit project")
            raise DeploymentNotFound(f"Deployment '{deployment_name}' not found in project '{project_name}'")

        if presented_token is not None:
            await _verify_token(project_data, deployment_name, presented_token)

        cluster = deployment.get("cluster", "")
        config = sleep_config.load(project_data, cluster)
        current = sleep_state.read(project_data, deployment_name)
        if config is None or not config.matches(deployment_name):
            steps.note("Slaapstand geldt niet voor deze deployment, er is niets gewijzigd")
            return WakeResult(changed=False, state=current.state)

        waking_timeout = timedelta(minutes=settings.SLEEP_MODE_WAKING_TIMEOUT_MINUTES)
        if not service.begin_wake(project_data, deployment_name, now, waking_timeout):
            new = sleep_state.read(project_data, deployment_name)
            steps.note(f"Geen wijziging nodig, de deployment is al {new.state}")
            return WakeResult(changed=False, state=new.state)

        steps.begin("Wektoestand vastleggen in git")
        await project_manager.save_and_commit_project(
            project_data, f"sleep-mode: wake {deployment_name} in {project_name}"
        )
        steps.done()
        # Only this one deployment is regenerated; Application/AppProject are untouched.
        # The reprocessing names its own steps on the same task.
        await trigger_reprocessing(
            project_name,
            filename,
            deployment_name,
            argocd_resources_changed=False,
            task_progress_manager=progress,
        )
        logger.info("sleep-mode: began waking %s/%s", project_name, deployment_name)
        return WakeResult(changed=True, state=sleep_state.STATE_WAKING)
    finally:
        await project_manager.close()


async def sleep(project_name: str, deployment_name: str, *, progress: Any | None = None) -> WakeResult:
    """Manually move a matching, awake deployment to ``sleeping`` and reprocess it.

    The counterpart of ``wake``: the same load -> transition -> commit -> reprocess
    sequence, but ``awake -> sleeping`` (minting a wake token when a waker will exist,
    exactly like the sweeper's automatic SLEEP). Idempotent: only ``awake`` applies;
    already sleeping/waking, or a non-matching deployment, is a no-op with no commit.
    This is session-authenticated (admin/owner), so there is no wake token to verify.

    ``progress`` is the task progress manager when a user is watching this run; it names
    the steps as they happen. The sweeper passes nothing and nothing is reported.
    """
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.manager.project_manager import ProjectManager
    from opi.services.catalog.sleep_mode import config as sleep_config
    from opi.services.catalog.sleep_mode import manifests, service
    from opi.services.catalog.sleep_mode import state as sleep_state
    from opi.services.catalog.sleep_mode import token as token_mod
    from opi.services.project_store import get_project_store
    from opi.services.resource_tuning_service import trigger_reprocessing

    steps = _Steps(progress)
    project = get_project_store().get(project_name)
    if not project:
        raise DeploymentNotFound(f"Project '{project_name}' not found")
    filename = project.filename

    project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")
    try:
        steps.begin("Projectgegevens ophalen")
        project_data = await project_manager.get_contents()
        deployment = _find_deployment(project_data, deployment_name)
        if deployment is None:
            steps.failed(f"Deployment '{deployment_name}' bestaat niet in dit project")
            raise DeploymentNotFound(f"Deployment '{deployment_name}' not found in project '{project_name}'")

        cluster = deployment.get("cluster", "")
        config = sleep_config.load(project_data, cluster)
        current = sleep_state.read(project_data, deployment_name)
        if config is None or not config.matches(deployment_name):
            steps.note("Slaapstand geldt niet voor deze deployment, er is niets gewijzigd")
            return WakeResult(changed=False, state=current.state)

        # Mint a wake token only when a waker will actually be generated, mirroring the
        # sweeper's SLEEP branch, so the deployment can be woken from its own page later.
        encrypted_token = None
        if config.waker:
            component = manifests.select_waker_component(project_data, deployment, config, ProjectFileHandler())
            if component is not None:
                encrypted_token = token_mod.encrypt(token_mod.mint(), project_data)

        if not service.to_sleeping(project_data, deployment_name, encrypted_token):
            new = sleep_state.read(project_data, deployment_name)
            steps.note(f"Geen wijziging nodig, de deployment is al {new.state}")
            return WakeResult(changed=False, state=new.state)

        steps.begin("Slaaptoestand vastleggen in git")
        await project_manager.save_and_commit_project(
            project_data, f"sleep-mode: sleep {deployment_name} in {project_name}"
        )
        steps.done()
        # Only this one deployment is regenerated; Application/AppProject are untouched.
        # The reprocessing names its own steps on the same task.
        await trigger_reprocessing(
            project_name,
            filename,
            deployment_name,
            argocd_resources_changed=False,
            task_progress_manager=progress,
        )
        logger.info("sleep-mode: manually put %s/%s to sleep", project_name, deployment_name)
        return WakeResult(changed=True, state=sleep_state.STATE_SLEEPING)
    finally:
        await project_manager.close()


async def status(project_name: str, deployment_name: str, *, presented_token: str | None = None) -> dict:
    """Read-only status the waker polls: ``{"state": "starting" | "ready"}``.

    Reads the cached project data (this is hot, every few seconds) and asks kubectl
    whether the app component behind the waker has a ready pod. ``ready`` tells the waker
    to step out of the EndpointSlice.
    """
    from opi.connectors.kubectl import KubectlConnector
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.services.catalog.sleep_mode import config as sleep_config
    from opi.services.catalog.sleep_mode import manifests
    from opi.services.project_store import get_project_store
    from opi.utils.naming import generate_unique_name

    project = get_project_store().get(project_name)
    if not project or not project.data:
        raise DeploymentNotFound(f"Project '{project_name}' not found")
    project_data = project.data
    deployment = _find_deployment(project_data, deployment_name)
    if deployment is None:
        raise DeploymentNotFound(f"Deployment '{deployment_name}' not found in project '{project_name}'")

    if presented_token is not None:
        await _verify_token(project_data, deployment_name, presented_token)

    cluster = deployment.get("cluster", "")
    config = sleep_config.load(project_data, cluster)
    handler = ProjectFileHandler()
    component = None
    if config is not None:
        component = manifests.select_waker_component(project_data, deployment, config, handler)
    if component is None:
        # No waker component known; report starting so the waker keeps its page.
        return {"state": "starting"}

    unique_name = generate_unique_name(deployment_name, component)
    namespace = get_prefixed_namespace(cluster, deployment.get("namespace", ""))
    statuses = await KubectlConnector().get_deployment_status(namespace, unique_name)
    ready = 0
    if statuses:
        raw = statuses[0].get("ready", "0")
        ready = int(raw.split("/")[0]) if "/" in raw else int(raw)
    return {"state": "ready" if ready > 0 else "starting"}
