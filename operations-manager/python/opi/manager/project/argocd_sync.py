"""Waiting for ArgoCD and clearing the sync operations that block a next sync."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from opi.connectors import create_argo_connector
from opi.connectors.argo import ArgoTransportError
from opi.core import progress_steps as steps
from opi.core.cluster_config import get_argo_namespace
from opi.core.config import settings
from opi.core.metrics import (
    PHASE_ARGO_PROJECT_GATE,
    PHASE_ARGO_UMBRELLA,
    rollout_phase,
    umbrella_refresh_lost_total,
)
from opi.manager.argo_manager import (
    APPLICATIE_AANMAAK_TIMEOUT_SECONDEN,
    ARGOCD_HERSTEL_BUDGET_SECONDEN,
    ARGOCD_HERSTEL_POLL_SECONDEN,
    PROJECT_LEVEL_SYNC_TIMEOUT_SECONDEN,
    UMBRELLA_REFRESH_MAX_POGINGEN,
    UMBRELLA_REFRESH_MIN_INTERVAL_SECONDEN,
)
from opi.services.argocd_operation import STUCK_OPERATION_THRESHOLD_SECONDS, terminate_stuck_operation
from opi.utils.naming import generate_argocd_application_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from opi.connectors.argo import ArgoConnector
    from opi.connectors.git_repo import GitRepoWorkspace
    from opi.connectors.kubectl import KubectlConnector
    from opi.manager.argo_manager import ArgoManager

logger = logging.getLogger(__name__)


class ArgoSyncAccess(Protocol):
    """What the ArgoCD waits reach from their host.

    ``_terminated_sync_operations`` is appended to and read back by the host after the call,
    so the list stays there; a group is built per call and would forget it. The three
    ``_project_level_*`` attributes are written by the manifest generation and read here.
    """

    _argo_manager: ArgoManager
    _kubectl_connector: KubectlConnector
    _terminated_sync_operations: list[str]
    _project_level_manifests_changed: bool
    _project_level_repo: str | None
    _project_level_path: str | None

    async def get_name(self) -> str: ...

    def get_existing_deployment_workspace(self, name: str) -> GitRepoWorkspace | None: ...

    def _begin_step(self, name: str, subject: str | None = None) -> str | None: ...

    def _end_step(self, step_id: str | None, error: str | None = None) -> None: ...


class ArgoCDSync:
    """The ArgoCD waits of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ArgoSyncAccess) -> None:
        self._host = host

    async def _keep_umbrella_refreshed(self, argo_connector: ArgoConnector, pushed_commit: str | None) -> None:
        """Poke `user-applications` again for as long as it has not seen our commit.

        A refresh is no queued command but a flag on the Application: if a reconcile was
        already running, it clears the flag when it finishes, while it fetched its revision
        before we pushed. Our change is then missing from it, and until the next reconcile
        (on odcn-production `timeout.reconciliation: 15m`) nothing more happens.

        Only the revision proves anything: `reconciledAt` may well lie after our push while
        the compared revision predates it. Without a known commit there is nothing to prove,
        so it then stops after the first refresh.

        The loop runs on the answer of the refresh and not on a clock: that call stays open
        until a reconcile has finished, which is exactly when its revision becomes visible.
        Was it the wrong one, then our flag was consumed by someone else's run and poking
        again right away pays off.

        Runs alongside the waits and is cancelled by the caller.
        """
        for attempt in range(1, UMBRELLA_REFRESH_MAX_POGINGEN + 1):
            started = time.monotonic()
            if not await argo_connector.refresh_application("user-applications"):
                logger.warning(
                    "Refresh van user-applications leverde niets op; een volgende ronde probeert het opnieuw"
                )
            if not pushed_commit:
                return

            status_data = await argo_connector.get_application_status("user-applications") or {}
            status = status_data.get("status", {}) or {}
            revision = status.get("sync", {}).get("revision")
            self._host._argo_manager.last_umbrella_revision = revision
            self._host._argo_manager.last_umbrella_reconciled_at = status.get("reconciledAt")

            if revision == pushed_commit:
                logger.info(f"user-applications heeft onze commit {pushed_commit} vergeleken; niet opnieuw verversen")
                return

            if attempt == UMBRELLA_REFRESH_MAX_POGINGEN:
                break

            umbrella_refresh_lost_total.inc()
            logger.info(
                f"user-applications staat nog op revisie {revision or 'onbekend'} in plaats van {pushed_commit}; "
                "de vorige refresh is verloren gegaan, opnieuw verversen"
            )
            # The refresh above waited for a reconcile itself; came it back at once anyway,
            # then this floor keeps it a loop instead of a storm.
            rest = UMBRELLA_REFRESH_MIN_INTERVAL_SECONDEN - (time.monotonic() - started)
            if rest > 0:
                await asyncio.sleep(rest)

        logger.warning(
            f"user-applications staat na {UMBRELLA_REFRESH_MAX_POGINGEN} refreshes nog op revisie "
            f"{self._host._argo_manager.last_umbrella_revision or 'onbekend'} in plaats van {pushed_commit}; "
            "hier is meer aan de hand dan een verloren wekker, doorprikken helpt niet"
        )

    @asynccontextmanager
    async def _umbrella_verversbewaker(self, argo_connector: ArgoConnector) -> AsyncIterator[None]:
        """Run `_keep_umbrella_refreshed` alongside a wait and clean it up afterwards.

        Every wait on the EXISTENCE of a child Application needs this watcher: our refresh
        can be absorbed by a reconcile that already fetched its revision, and then nothing
        happens until the next reconcile. One watcher per wait, also when that wait covers
        several applications at once: three watchers would refresh three times over.

        The broad ``except`` belongs here and not with the callers: the watcher is a safety
        net and must never drag down the wait it guards, whatever it falls over on itself.
        """
        watcher = asyncio.create_task(
            self._keep_umbrella_refreshed(argo_connector, self._host._argo_manager.last_pushed_argo_commit)
        )
        try:
            # The phase is measured here and not in the watcher: this wait is what the task
            # costs, and the watcher is always cut off halfway.
            with rollout_phase(PHASE_ARGO_UMBRELLA):
                yield
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Verversbewaker voor user-applications gestopt: {e}")

    async def wait_for_project_level_application(self, app_name: str) -> None:
        """Wait until the PROJECT LEVEL of this project is really synchronised.

        The ServiceAccount of the project comes from this application, and the podspec of
        every Deployment below it names it. Hence this wait comes before creating the
        deployment applications: while their CR does not exist, ArgoCD cannot synchronise
        them on its own either.

        Same shape as the infrastructure application, which is on wave 0 as well: create,
        wait until ArgoCD has created it, refresh it OURSELVES, and then wait for a sync that
        provably comes AFTER that refresh. Lacking that last part, a ``Synced`` from before
        our commit satisfies the wait at once on a repeated run.

        The sync wave does not cover this: a freshly created child Application holds zero
        resources and reports itself Healthy within a second. See
        `docs/post-mortems/de-serviceaccount-komt-na-de-deployment.md`.

        Args:
            app_name: Name of the project-level application.

        Raises:
            RuntimeError: If ArgoCD is unreachable or the refresh yields nothing.
            TimeoutError: If the application is not synchronised within the time.
        """
        with rollout_phase(PHASE_ARGO_PROJECT_GATE):
            if await self._project_level_already_applied(app_name):
                logger.info(
                    "Projectniveau '%s' staat al Synced en Healthy op onze stand; niet opnieuw verversen", app_name
                )
                return
            await self._await_project_level_application(app_name)

    @staticmethod
    async def _status_for_skip_decision(argo_connector: ArgoConnector, app_name: str) -> dict[str, Any]:
        """The status of an application, for a DECISION to skip work.

        Such a decision may never cost a task. ArgoCD answers 400 while the AppProject of a
        project does not exist or the application is not yet admitted into it, and
        ``get_application_status`` turns that into a RuntimeError: exactly the situation in
        which there is nothing to prove and the full run belongs to happen.
        """
        try:
            return await argo_connector.get_application_status(app_name) or {}
        except (ArgoTransportError, RuntimeError, OSError) as error:
            logger.info("Geen status van '%s' om werk op over te slaan: %s", app_name, error)
            return {}

    async def _render_already_proven(
        self, argo_connector: ArgoConnector, app_name: str, reconciled_at: str | None
    ) -> bool:
        """Whether this application reported Synced after our refresh, so its render held."""
        status_data = await self._status_for_skip_decision(argo_connector, app_name)
        if (status_data.get("status", {}) or {}).get("sync", {}).get("status") != "Synced":
            return False
        return self._host._argo_manager.status_reflects_refresh(status_data, reconciled_at)

    async def _project_level_already_applied(self, app_name: str) -> bool:
        """Whether ArgoCD has applied the project level as it stands in git right now.

        Three things have to hold, each of them proof and no assumption: this round left the
        project level untouched, the application reports Synced AND Healthy, and the folder
        holding the project-level manifests is, at the revision it compared, equal to what
        git holds now (``path_unchanged_since``). Then the ServiceAccount is in place, which
        is what this guard protects. Lacks one of the three, then the guard runs as before.

        See `docs/post-mortems/de-serviceaccount-komt-na-de-deployment.md`: a freshly created
        application reports itself Healthy holding zero resources, so Healthy on its own is
        never enough. The revision demand is what rules that case out: a new application has
        no compared revision yet.
        """
        if self._host._project_level_manifests_changed or self._host._project_level_repo is None:
            return False
        if self._host._project_level_path is None:
            return False
        connector = self._host.get_existing_deployment_workspace(self._host._project_level_repo)
        if connector is None:
            return False

        status_data = await self._status_for_skip_decision(create_argo_connector(), app_name)
        status = status_data.get("status", {}) or {}
        sync = status.get("sync", {}) or {}
        if sync.get("status") != "Synced" or (status.get("health", {}) or {}).get("status") != "Healthy":
            return False
        return await connector.path_unchanged_since(str(sync.get("revision") or ""), self._host._project_level_path)

    async def _await_project_level_application(self, app_name: str) -> None:
        """The wait itself; see :meth:`wait_for_project_level_application`."""
        argo_connector = create_argo_connector()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ARGOCD_HERSTEL_BUDGET_SECONDEN

        async def _keep_trying[T](what: str, attempt: Callable[[], Awaitable[T]]) -> T:
            """Retry until result or budget exhausted. Empty result means not ready."""
            while True:
                outcome = await attempt()
                if outcome:
                    return outcome
                over = deadline - loop.time()
                if over <= 0:
                    raise RuntimeError(
                        f"{what} within the {ARGOCD_HERSTEL_BUDGET_SECONDEN}s budget for a recovering ArgoCD"
                    )
                logger.warning("%s: opnieuw proberen, %ds van het budget over", what, int(over))
                await asyncio.sleep(ARGOCD_HERSTEL_POLL_SECONDEN)

        await _keep_trying("Failed to login to ArgoCD", argo_connector.login)

        # Refresh the umbrella only while the application is absent: such a refresh redraws
        # all child apps (issue #130) and can take minutes
        # (features/argocd-refresh-performance.md). Same choice, source and fail-safe as with
        # the deployment applications further on.
        exists = await self._host._kubectl_connector.argocd_application_exists(
            app_name, get_argo_namespace(settings.CLUSTER_MANAGER)
        )
        if exists is not True:
            async with self._umbrella_verversbewaker(argo_connector):
                await self._host._argo_manager.wait_for_application_created(
                    app_name=app_name,
                    timeout=APPLICATIE_AANMAAK_TIMEOUT_SECONDEN,
                    poll_interval=1,
                )

        # Do always refresh THIS application: without a sync that follows our own refresh, a
        # ``Synced`` proves nothing about the commit we just pushed.
        reconciled_at = await _keep_trying(
            f"Failed to refresh ArgoCD project-level application '{app_name}'",
            lambda: argo_connector.refresh_application(app_name),
        )

        await self._host._argo_manager.wait_for_application_synced(
            app_name=app_name,
            timeout=PROJECT_LEVEL_SYNC_TIMEOUT_SECONDEN,
            refreshed_after=reconciled_at,
        )
        logger.info(f"Project-level application '{app_name}' is synced; de ServiceAccount staat er")

    async def _terminate_operation_we_waited_on(self, app_name: str, cluster: str | None) -> None:
        """Terminate the operation that OPI's own wait has just run out on.

        Without a threshold, unlike the round before the push: that operation is YOUNGER than
        the threshold by construction, because ``wait_for_application_synced`` starts its
        clock only after the refresh and the render, and ArgoCD begins the operation after
        that. With the default threshold, exactly the blockade this branch has to clear
        stayed in place. And "still busy" is no possibility here any more: OPI has just
        waited 300s on this operation and given up. That it is still attached and still
        running (``blocks_next_sync``) is thereby the only question left.

        Leaving it makes a failed rollout a blockade on every following one as well.

        Does terminating fail, then the timeout is still reported: the call stands for
        ``fail_subtask`` and for the ``timeout`` outcome, and a kubectl that fell over may
        not make a ``KubectlConnectionError`` out of that. ``terminate_stuck_operation``
        catches that itself.
        """
        await terminate_stuck_operation(
            self._host._kubectl_connector,
            app_name,
            get_argo_namespace(cluster or settings.CLUSTER_MANAGER),
            threshold_seconds=0,
        )

    async def _clear_blocking_sync_operations(
        self, deployments: list[dict[str, Any]], reset_running_sync: bool = False
    ) -> None:
        """Terminate the sync operations blocking a next sync on these deployments.

        Normally WITH the threshold: this round runs before every push and looks at
        operations another task or a sweep left behind, so a rollout that is still busy stays
        in place.

        ``reset_running_sync`` puts the threshold at 0: on a refresh someone pressed reset,
        and then "still busy" is no reason to wait any more. Same rule as on the timeout
        branch (``_terminate_operation_we_waited_on``). The flag sits at the endpoint, so an
        internal caller does not inherit it silently.
        """
        project_name = await self._host.get_name()
        if not project_name:
            return
        threshold = 0 if reset_running_sync else STUCK_OPERATION_THRESHOLD_SECONDS
        terminated: list[str] = []
        for deployment in deployments:
            dep_name = deployment.get("name")
            cluster = deployment.get("cluster")
            if not dep_name or not cluster:
                continue
            if await terminate_stuck_operation(
                self._host._kubectl_connector,
                generate_argocd_application_name(project_name, str(dep_name)),
                get_argo_namespace(str(cluster)),
                threshold_seconds=threshold,
            ):
                terminated.append(str(dep_name))
        if not terminated:
            return
        self._host._terminated_sync_operations.extend(terminated)
        # The step begins and ends in one go: the work is already done.
        self._host._end_step(self._host._begin_step(steps.running_sync_terminated(terminated)))
