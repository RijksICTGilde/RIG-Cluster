"""The sleep-mode sweeper: put expired deployments to sleep, un-stick waking ones.

Interval-driven (like the db-console reaper): every ``SLEEP_MODE_SWEEP_MINUTES`` it walks
the project store for this cluster and, per deployment, either
- ``awake`` + matches + deadline passed  -> ``sleeping`` (minting a wake token if a waker
  will exist), or
- ``waking`` past its stuck-deadline      -> ``awake`` (a broken image never came back).

The decision is a pure function (``plan_sweep``); the loop applies it, committing once per
project and pacing between projects so twenty expired previews do not fire twenty commits
at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from opi.core.config import settings
from opi.services.catalog.sleep_mode import config as sleep_config
from opi.services.catalog.sleep_mode import service
from opi.services.catalog.sleep_mode import state as sleep_state
from opi.services.catalog.sleep_mode.state import STATE_AWAKE, STATE_WAKING

logger = logging.getLogger(__name__)

SLEEP = "sleep"
REVERT = "revert"
STAMP = "stamp"
#: A waking deployment that has not timed out: check if the app is back and, if so,
#: finish the wake (waking -> awake) promptly so the waker is pruned instead of
#: lingering until the timeout.
CHECK_AWAKE = "check-awake"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def decide_action(state: str, expires_at: str | None, matches: bool, now: datetime) -> str | None:
    """Pure per-deployment decision: STAMP, SLEEP, REVERT, or None (leave it alone).

    A matching awake deployment with no deadline yet gets one stamped (this is how a
    freshly created deployment acquires its first sleep deadline, without hooking every
    create path); once the deadline passes it sleeps.
    """
    expiry = _parse_iso(expires_at)
    if state == STATE_AWAKE and matches:
        if expiry is None:
            return STAMP
        if expiry < now:
            return SLEEP
    if state == STATE_WAKING:
        # Timed out (image never came back) -> revert. Otherwise check whether the app
        # is back so we can finish the wake promptly instead of waiting out the timeout.
        if expiry is not None and expiry < now:
            return REVERT
        return CHECK_AWAKE
    return None


def plan_sweep(project_data: dict, cluster: str, now: datetime) -> list[tuple[str, str]]:
    """The (deployment_name, action) list for one project on one cluster (pure).

    Returns nothing when sleep-mode is off for the project/cluster, so a project that
    never opted in costs one config load and no writes.
    """
    config = sleep_config.load(project_data, cluster)
    if config is None:
        return []
    plan: list[tuple[str, str]] = []
    for deployment in project_data.get("deployments", []) or []:
        if deployment.get("cluster") != cluster:
            continue
        name = deployment.get("name", "")
        current = sleep_state.read(project_data, name)
        action = decide_action(current.state, current.expires_at, config.matches(name), now)
        if action is not None:
            plan.append((name, action))
    return plan


class SleepModeScheduler:
    """Interval sweeper, one per OPI instance (its ``CLUSTER_MANAGER`` cluster)."""

    def __init__(self, cluster: str) -> None:
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Sleep-mode sweeper started (cluster=%s, every %d min)",
            self._cluster,
            settings.SLEEP_MODE_SWEEP_MINUTES,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Sleep-mode sweeper stopped")

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(settings.SLEEP_MODE_SWEEP_MINUTES * 60)
            except asyncio.CancelledError:
                break
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in sleep-mode sweep")

    async def sweep_once(self, now: datetime | None = None) -> None:
        from opi.services.project_store import get_project_store

        now = now or datetime.now(UTC)
        for project in get_project_store().get_all():
            data = getattr(project, "data", None)
            if not data:
                continue
            plan = plan_sweep(data, self._cluster, now)
            if not plan:
                continue
            changed = await self._apply(project.name, project.filename, plan, now)
            if changed:
                # Pace between projects so many expiries do not fire many commits at once.
                await asyncio.sleep(settings.SLEEP_MODE_PACE_SECONDS)

    async def _app_is_ready(self, project_data: dict, deployment: dict, config) -> bool:
        """Whether the app behind the waker has a ready pod (so waking can finish)."""
        from opi.connectors.kubectl import KubectlConnector
        from opi.core.cluster_config import get_prefixed_namespace
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.services.catalog.sleep_mode import manifests
        from opi.utils.naming import generate_unique_name

        component = manifests.select_waker_component(project_data, deployment, config, ProjectFileHandler())
        if component is None:
            # No waker component: nothing serves a page, so a bare wake is done immediately.
            return True
        unique_name = generate_unique_name(deployment.get("name", ""), component)
        namespace = get_prefixed_namespace(deployment.get("cluster", ""), deployment.get("namespace", ""))
        statuses = await KubectlConnector().get_deployment_status(namespace, unique_name)
        if not statuses:
            return False
        raw = statuses[0].get("ready", "0")
        ready = int(raw.split("/")[0]) if "/" in raw else int(raw)
        return ready > 0

    async def _apply(self, project_name: str, filename: str, plan: list[tuple[str, str]], now: datetime) -> bool:
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.manager.project_manager import ProjectManager
        from opi.services.catalog.sleep_mode import manifests, token
        from opi.services.resource_tuning_service import trigger_reprocessing

        project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")
        applied: list[str] = []
        try:
            project_data = await project_manager.get_contents()
            handler = ProjectFileHandler()
            for deployment_name, action in plan:
                deployment = next(
                    (d for d in project_data.get("deployments", []) if d.get("name") == deployment_name), None
                )
                if deployment is None:
                    continue
                if action == STAMP:
                    config = sleep_config.load(project_data, deployment.get("cluster", ""))
                    if config is not None and service.set_sleep_deadline(
                        project_data, deployment_name, now, config.sleep_after_deploy_delta
                    ):
                        applied.append(deployment_name)
                        logger.info(
                            "sleep-mode: stamped initial sleep deadline for %s/%s", project_name, deployment_name
                        )
                elif action == SLEEP:
                    config = sleep_config.load(project_data, deployment.get("cluster", ""))
                    encrypted_token = None
                    if config is not None and config.waker:
                        component = manifests.select_waker_component(project_data, deployment, config, handler)
                        if component is not None:
                            encrypted_token = token.encrypt(token.mint(), project_data)
                    if service.to_sleeping(project_data, deployment_name, encrypted_token):
                        applied.append(deployment_name)
                        logger.info("sleep-mode: sleeping %s/%s (deadline passed)", project_name, deployment_name)
                elif action == REVERT:
                    sleep_after_wake = timedelta(hours=1)
                    config = sleep_config.load(project_data, deployment.get("cluster", ""))
                    if config is not None:
                        sleep_after_wake = config.sleep_after_wake_delta
                    if service.to_awake(project_data, deployment_name, now, sleep_after_wake):
                        applied.append(deployment_name)
                        logger.warning(
                            "sleep-mode: reverting stuck waking %s/%s to awake", project_name, deployment_name
                        )
                elif action == CHECK_AWAKE:
                    config = sleep_config.load(project_data, deployment.get("cluster", ""))
                    if config is None:
                        continue
                    if not await self._app_is_ready(project_data, deployment, config):
                        continue
                    if service.to_awake(project_data, deployment_name, now, config.sleep_after_wake_delta):
                        applied.append(deployment_name)
                        logger.info(
                            "sleep-mode: finished waking %s/%s (app is back), pruning waker",
                            project_name,
                            deployment_name,
                        )

            if not applied:
                return False
            await project_manager.save_and_commit_project(
                project_data, f"sleep-mode: sweep {project_name} ({', '.join(applied)})"
            )
            await trigger_reprocessing(project_name, filename, None, argocd_resources_changed=False)
            return True
        finally:
            await project_manager.close()
