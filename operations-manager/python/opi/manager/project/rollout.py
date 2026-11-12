"""Rolling out one round's ArgoCD applications: refresh each, wait for it, report what came back."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import asyncpg
from sqlalchemy.exc import SQLAlchemyError

from opi.connectors import create_argo_connector
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.core.metrics import PHASE_AFTER_SYNC, rollout_phase
from opi.manager.argo_manager import APPLICATION_CREATION_TIMEOUT_SECONDS, ApplicationGoneError
from opi.services.argocd_operation import describe_sync_waiting
from opi.services.deployment_state import collect_deployment_state
from opi.services.event_interpreter import _interpret_by_reason, condense_render_error
from opi.services.project_file_handler import image_is_confirmed_absent
from opi.services.tasks import progress_steps as steps
from opi.services.tasks.async_task_service import AsyncTaskService
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_project_application_name,
    generate_unique_name,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from opi.connectors.argo import ArgoConnector
    from opi.connectors.kubectl import KubectlConnector
    from opi.manager.argo_manager import ArgoManager
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager

logger = logging.getLogger(__name__)

#: How many unhealthy deployments the closing notice names before it counts the rest.
UNHEALTHY_NAMES_SHOWN = 5

#: How long a single application may take to become Synced and Healthy.
APPLICATION_SYNC_TIMEOUT_SECONDS = 300

#: Interval of the status poll. The component inspection in ``on_poll`` keeps its own,
#: slower rhythm so the cheap status read does not drag the cluster along.
APPLICATION_SYNC_POLL_SECONDS = 2

#: Seconds between two kubectl inspections of the components of one deployment.
COMPONENT_DESCRIBE_INTERVAL_SECONDS = 5

_TYPE_TO_REASON = {
    "crash_loop": "CrashLoopBackOff",
    "image_pull": "ImagePullBackOff",
    "oom": "OOMKilled",
}

# Registry auth/permission failures (missing or expired pull secret, denied robot)
# are real, actionable errors worth an ERROR log + alert. A not-found / not-yet-built
# image is expected churn for CI/CD and PR builds and belongs at WARNING, not ERROR
# ("ERROR" should mean someone must act). We match only unambiguous auth markers;
# a bare "denied" is left out because some registries (e.g. ghcr) return it for a
# private-or-missing tag too, which would misclassify a not-built image as an error.
_IMAGE_PULL_AUTH_MARKERS = (
    "unauthorized",
    "authentication required",
    "authorization failed",
    "no basic auth credentials",
    "forbidden",
    "401",
    "403",
)

# Markers that identify an ArgoCD manifest-generation failure in a manifests-endpoint body.
# These are the phrases ArgoCD's repo-server uses when a plugin (our SOPS/kustomize CMP)
# cannot render, so they distinguish a real render failure from a transport/auth error (a
# 401/404/network hiccup) that must not block the deploy.
_RENDER_FAILURE_MARKERS = (
    "failed to generate manifests",
    "error generating manifests",
    "manifest generation error",
    "rpc error",
)


def is_image_pull_auth_error(message: str | None) -> bool:
    """True when an image-pull failure is a registry auth/permission problem."""
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _IMAGE_PULL_AUTH_MARKERS)


def looks_like_render_failure(detail: str | None) -> bool:
    """True when a failed manifests-endpoint body indicates a generation/render error."""
    if not detail:
        return False
    lowered = detail.lower()
    return any(marker in lowered for marker in _RENDER_FAILURE_MARKERS)


async def deployments_being_deleted(project_name: str) -> dict[str, str]:
    """Deployments with an open delete task, mapped to that task's id (RC-254).

    A failed query yields nothing: the round then regenerates as it did before this gate,
    which beats not processing a project because the task table stalled.
    """
    try:
        return await AsyncTaskService(cluster=settings.CLUSTER_MANAGER).find_open_delete_deployments(project_name)
    except (SQLAlchemyError, OSError, asyncpg.exceptions.PostgresError, asyncpg.exceptions.InterfaceError) as e:
        logger.warning("Could not query the open delete tasks of project %s: %s", project_name, e)
        return {}


class DeploymentRolloutAccess(Protocol):
    """What the rollout reaches from its host.

    ``_component_failures`` and ``_processing_error`` are read back by the host after the
    call, so they stay there; a group is built per call and would forget them.
    """

    _argo_manager: ArgoManager
    _kubectl_connector: KubectlConnector
    _component_failures: list[dict[str, Any]] | None
    _processing_error: str | None

    async def get_name(self) -> str: ...

    async def get_deployments(
        self,
        cluster_filter: bool = True,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]: ...

    def get_progress_manager(self) -> PersistentTaskProgressManager | None: ...

    def _begin_phase(self, name: str) -> str | None: ...

    def _umbrella_refresh_watcher(self, argo_connector: ArgoConnector) -> AbstractAsyncContextManager[None]: ...

    async def _render_already_proven(
        self, argo_connector: ArgoConnector, app_name: str, reconciled_at: str | None
    ) -> bool: ...

    async def _terminate_operation_we_waited_on(self, app_name: str, cluster: str | None) -> None: ...

    async def _queue_refresh_task(
        self, task_service: AsyncTaskService | None, project_name: str, deployment_name: str
    ) -> None: ...


@dataclass(frozen=True)
class RolloutRound:
    """What every application of one round is rolled out against."""

    argo_connector: ArgoConnector
    argo_task: str | None
    progress_manager: PersistentTaskProgressManager | None
    project_name: str
    project_data: dict[str, Any]
    wait_for_healthy: bool


@dataclass
class RolloutTally:
    """What the round collected while it waited on its applications.

    ``health_warnings`` carries the raw, full text: that goes to the LOG, where all the
    detail belongs. What the USER gets is built from these below, as a count; the
    per-component story is in ``component_failures``, with a translated title.
    """

    sync_failures: list[str] = field(default_factory=list)
    health_warnings: list[str] = field(default_factory=list)
    unhealthy_deployments: list[str] = field(default_factory=list)
    component_failures: list[dict[str, Any]] = field(default_factory=list)
    registry_outage_seen: bool = False
    synced_deployment_count: int = 0


class DeploymentRollout:
    """The ArgoCD rollout of ``process_project_from_git``, built per call around its host."""

    def __init__(self, host: DeploymentRolloutAccess) -> None:
        self._host = host

    async def run(self, targets: list[str] | None, wait_for_healthy: bool) -> bool:
        """Refresh and wait for every in-scope application; False when the rollout failed."""
        logger.info("Triggering ArgoCD sync for user-applications and project applications after project processing")

        progress_manager = self._host.get_progress_manager()
        # The rollout phase is this step itself: its rows per deployment land under it.
        argo_task = self._host._begin_phase(steps.ROLLOUT)
        argo_connector = create_argo_connector()
        project_name = await self._host.get_name()
        deployments = await self._host.get_deployments(cluster_filter=True, deployment_names=targets)
        # process_project skipped these deployments (RC-254), so there is no
        # Application to wait on; the wait would only end on the creation time-out.
        being_deleted = await deployments_being_deleted(project_name)
        if being_deleted:
            deployments = [dep for dep in deployments if dep.get("name") not in being_deleted]

        round_ = RolloutRound(
            argo_connector=argo_connector,
            argo_task=argo_task,
            progress_manager=progress_manager,
            project_name=project_name,
            # The project as it stands, for asking the services what they know about a
            # deployment (RC-28). Same contents the deployments above came from.
            project_data=await self._host.get_contents(record_base=False),
            wait_for_healthy=wait_for_healthy,
        )
        tally = RolloutTally()

        if deployments and project_name:
            await self._roll_out(round_, deployments, tally)

        return self._report(round_, tally)

    async def _roll_out(self, round_: RolloutRound, deployments: list[dict[str, Any]], tally: RolloutTally) -> None:
        """Create what is missing, then refresh and wait for every application concurrently."""
        app_deployments = [
            (generate_argocd_application_name(round_.project_name, dep["name"]), dep)
            for dep in deployments
            if dep.get("name")
        ]
        app_names = [app_name for app_name, _ in app_deployments]
        logger.info(f"Waiting for {len(app_names)} ArgoCD applications to sync for {round_.project_name}")

        await self._wait_for_missing_applications(round_, app_deployments, tally)

        pending_apps = [(a, d) for a, d in app_deployments if not any(a in f for f in tally.sync_failures)]
        if round_.progress_manager and round_.argo_task and pending_apps:
            round_.progress_manager.update_task(
                round_.argo_task, steps.ROLLOUT, subject=steps.waiting_for_applications(len(pending_apps))
            )
        outcomes = await asyncio.gather(*(self._refresh_and_wait(round_, a, d) for a, d in pending_apps))
        tally.synced_deployment_count = len(outcomes)

        # Serial remediation pass over the outcomes. OOM tuning and image-pull
        # reporting write to the project file in git, so they must not run concurrently.
        self._host._component_failures = tally.component_failures
        for outcome in outcomes:
            await self._handle_outcome(round_, outcome, tally)

    async def _wait_for_missing_applications(
        self,
        round_: RolloutRound,
        app_deployments: list[tuple[str, dict[str, Any]]],
        tally: RolloutTally,
    ) -> None:
        """Refresh user-applications until every Application CR this round needs exists.

        A refresh re-renders all ~90 child apps (the umbrella lacks the
        manifest-generate-paths annotation, see issue #130), so it is slow and only worth
        it when a target Application CR is not yet present. Existence is checked against
        the Kubernetes API (ground truth); the ArgoCD API lies when its cache is stalled.
        Fail safe: anything not confirmed present (False or unknown None) triggers it.
        """
        existence = await asyncio.gather(
            *(
                self._host._kubectl_connector.argocd_application_exists(
                    app_name, get_argo_namespace(deployment.get("cluster", "local"))
                )
                for app_name, deployment in app_deployments
            )
        )
        app_names = [app_name for app_name, _ in app_deployments]
        apps_to_create = [app_names[i] for i, exists in enumerate(existence) if exists is not True]

        # The PROJECT-LEVEL application too: it is not in app_deployments.
        # `create_argocd_resources` already created and synced it through
        # `wait_for_project_level_application`, so normally this adds nothing; it stays
        # for the paths that arrive here without that gate.
        project_app_name = generate_argocd_project_application_name(round_.project_name)
        project_app_exists = await self._host._kubectl_connector.argocd_application_exists(
            project_app_name, get_argo_namespace(settings.CLUSTER_MANAGER)
        )
        if project_app_exists is not True:
            apps_to_create.append(project_app_name)

        if not apps_to_create:
            logger.info(f"All {len(app_names)} target application(s) already exist; skipping user-applications refresh")
            return

        logger.info(
            f"Refreshing user-applications: {len(apps_to_create)} application(s) not yet present "
            f"({', '.join(apps_to_create)})"
        )

        async def _wait_created(app_name: str) -> str | None:
            try:
                await self._host._argo_manager.wait_for_application_created(
                    app_name=app_name, timeout=APPLICATION_CREATION_TIMEOUT_SECONDS, poll_interval=1
                )
                return None
            except TimeoutError:
                logger.error(f"Timed out waiting for ArgoCD application '{app_name}' to be created")
                return f"{app_name}: timed out waiting for application to be created"

        # All waits run concurrently: read-only polls.
        async with self._host._umbrella_refresh_watcher(round_.argo_connector):
            created_results = await asyncio.gather(*(_wait_created(name) for name in apps_to_create))
        tally.sync_failures.extend(result for result in created_results if result)

        # Waiting on the project level is pointless here: the deployment applications
        # exist already and ArgoCD syncs them on its own. The ordering that matters sits
        # in `create_argocd_resources`, before they are created.

    async def _refresh_and_wait(
        self, round_: RolloutRound, app_name: str, deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """Refresh one application and wait for it; the outcome says how the wait ended.

        Refresh and wait run concurrently per application (read-only polls); the
        remediation over the outcomes stays serial because it mutates the project file.
        """
        # oom_watcher reaches back to ProjectManager through resource_tuning_service, so it
        # cannot be imported while this module is: that is a cycle.
        from opi.services.oom_watcher import (
            STALL_NOTICE_SECONDS,
            DeploymentHealthError,
            create_health_check_callback,
            describe_components_waiting,
        )

        progress_manager = round_.progress_manager
        dep_name = deployment.get("name", "")
        base_namespace = deployment.get("namespace", "")
        cluster = deployment.get("cluster", "")
        namespace = get_prefixed_namespace(cluster, base_namespace) if base_namespace and cluster else ""

        # What the services report about this deployment (RC-28): sleep-mode says it
        # scaled the application to zero, so "no pods" is the intended state and not
        # something to report as a stalled rollout.
        deployment_state = collect_deployment_state(round_.project_data, dep_name)

        oom_callback = None
        component_names: list[str] = []
        component_refs: dict[str, str] = {}
        if namespace:
            components = deployment.get("components", [])
            component_names = [
                generate_unique_name(dep_name, comp.get("reference", ""))
                for comp in components
                if comp.get("reference")
            ]
            component_refs = {
                generate_unique_name(dep_name, comp.get("reference", "")): comp.get("reference", "")
                for comp in components
                if comp.get("reference")
            }
            if component_names:
                oom_callback = create_health_check_callback(
                    round_.project_name,
                    dep_name,
                    namespace,
                    component_names,
                    component_refs=component_refs,
                    state=deployment_state,
                )

        # Live per-deployment progress subtask: surfaces "what are we waiting on" each
        # poll (which component, why), so a stalled rollout is no longer silent. None
        # without a progress manager (e.g. non-task callers).
        app_subtask = (
            progress_manager.add_subtask(round_.argo_task, dep_name, subject="uitrol voorbereiden…")
            if progress_manager and round_.argo_task
            else None
        )

        # Track the last reported line per deployment to detect stalls.
        stall: dict[str, Any] = {"line": None, "since": 0}
        describe_next: dict[str, int] = {"at": 0}

        async def _on_poll(elapsed: int, status: dict[str, Any], fresh: bool) -> None:
            if not (progress_manager and app_subtask):
                return
            # What ArgoCD itself is still doing comes before the pods: while the sync
            # is unfinished, pod state says nothing about what the rollout waits on.
            argo_reason = describe_sync_waiting(status, fresh=fresh)
            statuses: list[tuple[str, str]] = []
            if argo_reason:
                detail = argo_reason
            else:
                if elapsed < describe_next["at"]:
                    return
                describe_next["at"] = elapsed + COMPONENT_DESCRIBE_INTERVAL_SECONDS
                if namespace and component_names:
                    try:
                        statuses = await describe_components_waiting(
                            namespace, component_names, component_refs, deployment_state
                        )
                    except OSError, ValueError, KeyError, TypeError:
                        statuses = []
                # Components with the same reason on one line: two components waiting
                # on the same image are one thing going wrong, not two.
                by_reason: dict[str, list[str]] = {}
                for ref, reason in statuses:
                    by_reason.setdefault(reason, []).append(ref)

                if statuses:
                    detail = " · ".join(f"{', '.join(refs)}: {reason}" for reason, refs in by_reason.items())
                else:
                    detail = "wachten tot componenten gereed zijn"

            if detail != stall["line"]:
                stall["line"] = detail
                stall["since"] = elapsed
            waited = elapsed - stall["since"]
            if waited >= STALL_NOTICE_SECONDS and (argo_reason or statuses):
                detail = f"{detail} (al {waited}s onveranderd, bekijk de logs)"
            # The deployment name is the title of the row, the reason goes underneath.
            progress_manager.update_task(app_subtask, dep_name, subject=detail)

        # Failure detection (OOM / ImagePull / CrashLoop) runs after on_poll, so the user
        # still sees the live status before a DeploymentHealthError aborts the wait.
        on_poll = _on_poll if app_subtask else None

        try:
            reconciled_at = await round_.argo_connector.refresh_application(app_name)
            await self._prove_render(round_, app_name, reconciled_at)

            if not round_.wait_for_healthy:
                logger.info("Application '%s' refreshed and rendered; not waiting for Synced/Healthy", app_name)
                if app_subtask and progress_manager:
                    progress_manager.update_task(app_subtask, dep_name, subject="uitgerold")
                    progress_manager.complete_subtask(app_subtask)
                return {"app_name": app_name, "dep_name": dep_name, "status": "ok"}

            await self._host._argo_manager.wait_for_application_synced(
                app_name=app_name,
                timeout=APPLICATION_SYNC_TIMEOUT_SECONDS,
                poll_interval=APPLICATION_SYNC_POLL_SECONDS,
                refreshed_after=reconciled_at,
                on_progressing=oom_callback,
                on_poll=on_poll,
            )
            logger.info(f"Application '{app_name}' is synced and healthy")
            if app_subtask and progress_manager:
                progress_manager.update_task(app_subtask, dep_name, subject="uitgerold en gezond")
                progress_manager.complete_subtask(app_subtask)
            return {"app_name": app_name, "dep_name": dep_name, "status": "ok"}
        except DeploymentHealthError as e:
            logger.warning("Pod health issues during sync of '%s': %s", app_name, e)
            if app_subtask and progress_manager:
                comps = ", ".join(dict.fromkeys(f.component_reference or f.component_name for f in e.failures))
                # No update_task: the name IS the deployment name and the subject carries
                # the last observed reason. fail_subtask only adds the outcome; the full
                # story is in component_failures.
                logger.info("Deployment '%s' not healthy: %s", dep_name, comps)
                progress_manager.fail_subtask(app_subtask, "niet gezond geworden")
            return {"app_name": app_name, "dep_name": dep_name, "status": "health_error", "error": e}
        except TimeoutError:
            last_state = stall["line"] or "no progress reported"
            logger.error(
                f"Timed out after {APPLICATION_SYNC_TIMEOUT_SECONDS}s waiting for '{app_name}' ({dep_name}) "
                f"to sync; last observed state: {last_state}"
            )
            await self._host._terminate_operation_we_waited_on(app_name, cluster)
            if app_subtask and progress_manager:
                progress_manager.fail_subtask(app_subtask, "time-out na 300s")
            return {"app_name": app_name, "dep_name": dep_name, "status": "timeout"}
        except ApplicationGoneError as e:
            # Not a sync failure: another task (usually a delete_deployment for the same
            # deployment) removed the Application while we were waiting on it. There is
            # nothing left to report about a deployment that is gone.
            logger.info("Application '%s' (%s) verdween tijdens de wacht: %s", app_name, dep_name, e)
            if app_subtask and progress_manager:
                progress_manager.update_task(app_subtask, dep_name, subject="verwijderd tijdens de uitrol")
                progress_manager.complete_subtask(app_subtask)
            return {"app_name": app_name, "dep_name": dep_name, "status": "removed"}
        except RuntimeError as e:
            logger.error(f"Application '{app_name}' failed to sync: {e}")
            if app_subtask and progress_manager:
                progress_manager.fail_subtask(app_subtask, str(e))
            return {"app_name": app_name, "dep_name": dep_name, "status": "error", "error": e}

    async def _prove_render(self, round_: RolloutRound, app_name: str, reconciled_at: str | None) -> None:
        """Fetch the render right after our push, so a broken kustomization fails fast.

        The generation error, plugin stderr included, comes back here instead of after a
        300s time-out, and this also covers helm/helmfile deployments, which only render
        inside the CMP. A non-render failure (network, 401) must not block the deploy.

        An application reporting Synced AFTER our refresh has rendered already: ArgoCD
        cannot compare without manifests. Fetching them (the most expensive call of the
        loop, the whole render over the wire) then buys nothing, and the wait loop does
        that status-GET anyway.
        """
        if await self._host._render_already_proven(round_.argo_connector, app_name, reconciled_at):
            logger.debug("Render van '%s' al bewezen door een verse Synced; niet ophalen", app_name)
            return

        render_ok, render_detail = await round_.argo_connector.get_application_manifests(app_name)
        if not render_ok and looks_like_render_failure(render_detail):
            logger.error("Manifest render failed for '%s': %s", app_name, render_detail)
            raise RuntimeError(f"Manifest render failed for '{app_name}': {condense_render_error(render_detail)}")
        if not render_ok:
            logger.warning(
                "Could not verify render for '%s' (not a render error), proceeding to wait: %s",
                app_name,
                render_detail,
            )

    async def _handle_outcome(self, round_: RolloutRound, outcome: dict[str, Any], tally: RolloutTally) -> None:
        """Report and remediate one outcome. Writes the project file, so callers stay serial."""
        app_name = outcome["app_name"]
        dep_name = outcome["dep_name"]
        # "removed" is treated like "ok": the deployment is gone, so no sync_failures
        # and no health_warnings.
        if outcome["status"] in ("ok", "removed"):
            return
        if outcome["status"] == "timeout":
            tally.sync_failures.append(
                f"{app_name} ({dep_name}): timed out after {APPLICATION_SYNC_TIMEOUT_SECONDS}s waiting for sync"
            )
            return
        if outcome["status"] == "error":
            tally.sync_failures.append(f"{app_name}: {outcome['error']}")
            return

        error = outcome["error"]
        tally.component_failures.extend(_component_failure_rows(error.failures, dep_name))

        oom_failures = [f for f in error.failures if f.failure_type == "oom"]
        if oom_failures:
            await self._tune_after_oom(round_, app_name, dep_name, oom_failures, tally)

        # One list, one destination: reporting. The wording below still distinguishes an
        # absent image from an auth failure; nothing else does.
        _report_image_pull_failures(
            app_name, dep_name, [f for f in error.failures if f.failure_type == "image_pull"], tally
        )

        # CrashLoopBackOff is the user's app crashing at runtime, not a deploy/sync
        # failure: report it as a warning, don't fail the task.
        crash_loop_failures = [f for f in error.failures if f.failure_type == "crash_loop"]
        if crash_loop_failures:
            names = ", ".join(f.component_name for f in crash_loop_failures)
            tally.health_warnings.append(f"{app_name}: CrashLoopBackOff for {names}")
            if dep_name not in tally.unhealthy_deployments:
                tally.unhealthy_deployments.append(dep_name)

    async def _tune_after_oom(
        self,
        round_: RolloutRound,
        app_name: str,
        dep_name: str,
        oom_failures: list[Any],
        tally: RolloutTally,
    ) -> None:
        """Hand the OOM'd components to the after-sync hooks and queue a refresh if they tuned.

        No per-service branch here: the observation builds the observed health and lets the
        registry decide. The resource-tuning system service tunes and asks for a refresh;
        the runner commits once.
        """
        # Both reach back to ProjectManager through resource_tuning_service: a cycle.
        from opi.services.deployment_observation import run_after_sync_observation
        from opi.services.oom_watcher import component_health_from_oom_failures

        progress_manager = round_.progress_manager
        oom_names = ", ".join(f.component_reference or f.component_name for f in oom_failures)
        if progress_manager and round_.argo_task:
            progress_manager.update_task(
                round_.argo_task, steps.ROLLOUT, subject=steps.tuning_memory(oom_names, app_name)
            )
        task_service = (
            progress_manager._task_service if progress_manager and hasattr(progress_manager, "_task_service") else None
        )
        component_health = component_health_from_oom_failures(oom_failures)
        try:
            with rollout_phase(PHASE_AFTER_SYNC):
                observation = await run_after_sync_observation(round_.project_name, dep_name, component_health)
            if observation.requeue_refresh:
                logger.info(
                    "OOM auto-tune committed changes for %s/%s (components: %s), queuing refresh task",
                    round_.project_name,
                    dep_name,
                    oom_names,
                )
                await self._host._queue_refresh_task(task_service, round_.project_name, dep_name)
            tally.sync_failures.extend(f"{app_name}: {msg}" for msg in observation.failures)
        except Exception as tune_err:  # noqa: BLE001  # a step of this flow records its failure in the result
            logger.error(
                "OOM after-sync observation failed for %s/%s (components: %s): %s",
                round_.project_name,
                dep_name,
                oom_names,
                tune_err,
            )
            tally.sync_failures.append(f"{app_name}: OOM detected for {oom_names}, auto-tune failed: {tune_err}")

    def _report(self, round_: RolloutRound, tally: RolloutTally) -> bool:
        """Close the rollout phase and say whether it succeeded."""
        progress_manager = round_.progress_manager
        if tally.health_warnings:
            logger.warning(
                "Runtime pod-health issue(s) after sync (app crashing, not a deploy failure): %s",
                "; ".join(tally.health_warnings),
            )

        if tally.sync_failures:
            failure_summary = "; ".join(tally.sync_failures)
            logger.error(f"ArgoCD sync completed with {len(tally.sync_failures)} failure(s): {failure_summary}")
            if progress_manager and round_.argo_task:
                progress_manager.fail_task(round_.argo_task, f"Sync failures: {failure_summary}")
            self._host._processing_error = failure_summary
            return False

        if tally.health_warnings:
            # The deploy itself succeeded: manifests generated, pushed, and synced. The
            # app's own pods are crashing (CrashLoopBackOff, a not-yet-built PR image),
            # which is the tenant's problem, not ours. Complete the task and report the
            # reason, rather than returning a failure the caller alerts on.
            logger.info("ArgoCD applications synced (%d runtime health warning(s) above)", len(tally.health_warnings))
            if progress_manager and round_.argo_task:
                # The wait is over: its subject ("wacht op ...") would otherwise stay.
                progress_manager.update_task(round_.argo_task, steps.ROLLOUT, subject="")
                progress_manager.complete_task(round_.argo_task)
                notice = progress_manager.add_subtask(
                    round_.argo_task,
                    steps.deployed_but_unhealthy(*_unhealthy_counts(tally)),
                    subject=f"{_unhealthy_names(tally)}. {_unhealthy_cause(tally)}",
                )
                progress_manager.complete_task(notice)
            return True

        if round_.wait_for_healthy:
            logger.info("All ArgoCD applications are synced and healthy")
        else:
            logger.info("All ArgoCD applications are refreshed and rendered; health was not awaited")
        if progress_manager and round_.argo_task:
            progress_manager.update_task(round_.argo_task, steps.ROLLOUT, subject="")
            progress_manager.complete_task(round_.argo_task)
        return True


def _report_image_pull_failures(
    app_name: str, dep_name: str, image_pull_failures: list[Any], tally: RolloutTally
) -> None:
    """Turn image-pull failures into the sentence their reader can act on.

    A pod never becomes ready while any of its containers (main OR sidecar) is stuck
    pulling, so a sidecar failure is the component's problem too, and the message names
    the exact container and image. An answer about this image is the tenant's to act on;
    anything else is the platform's and must not send them looking for an image that is
    fine.
    """
    # oom_watcher reaches back to ProjectManager through resource_tuning_service: a cycle.
    from opi.services.oom_watcher import MAIN_CONTAINER_NAME

    for f in image_pull_failures:
        image = f" [image {f.image}]" if f.image else ""
        reason = " ".join(f.message.split()) if f.message else ""
        if not image_is_confirmed_absent(f.message):
            tally.registry_outage_seen = True
            tally.health_warnings.append(
                f"{app_name}: de registry kon het image voor component "
                f"'{f.component_name}'{image} niet leveren: {reason}. "
                "Het component blijft aan staan en probeert het opnieuw."
            )
            if dep_name not in tally.unhealthy_deployments:
                tally.unhealthy_deployments.append(dep_name)
            continue
        if f.container_name and f.container_name != MAIN_CONTAINER_NAME:
            target = f"sidecar '{f.container_name}' in component '{f.component_name}'"
        else:
            target = f"component '{f.component_name}'"
        detail = f"{target}{image}"
        if reason:
            detail = f"{detail}: {reason}"
        msg = f"{app_name}: ImagePullBackOff for {detail}"
        # Only a registry auth/permission failure is a real error worth failing the
        # task + alerting. A not-found / not-yet-built image is expected churn
        # (CI/CD, PR builds) -> warn. Deliberately asked only of a message the
        # registry answered: the auth markers include bare "401"/"403", which an
        # image tag can carry, and a transport outage must never fail a deploy on
        # the spelling of a tag.
        if is_image_pull_auth_error(f.message):
            tally.sync_failures.append(msg)
        else:
            tally.health_warnings.append(msg)
            if dep_name not in tally.unhealthy_deployments:
                tally.unhealthy_deployments.append(dep_name)


def _component_failure_rows(failures: list[Any], dep_name: str) -> list[dict[str, Any]]:
    """Per-component failure details for the task result, with a title and a suggestion.

    The severity comes from the translator: it is the difference between "you have to act
    on this" and "you do not".
    """
    rows: list[dict[str, Any]] = []
    for f in failures:
        translation = _interpret_by_reason(_TYPE_TO_REASON.get(f.failure_type, ""), f.message)
        title = translation[0] if translation else f"Component '{f.component_reference or f.component_name}'"
        rows.append(
            {
                "severity": translation[2].value if translation else "actionable",
                "component": f.component_reference or f.component_name,
                "deployment": f.deployment_name or dep_name,
                "failure_type": f.failure_type,
                "message": f.message,
                "title": title,
                "suggestion": translation[1] if translation else f.message,
                **({"logs": f.logs} if f.logs else {}),
                **({"container": f.container_name} if f.container_name else {}),
                **({"image": f.image} if f.image else {}),
            }
        )
    return rows


def _unhealthy_names(tally: RolloutTally) -> str:
    """The unhealthy deployments by name, the surplus above the first few as a count."""
    if len(tally.unhealthy_deployments) <= UNHEALTHY_NAMES_SHOWN:
        return ", ".join(tally.unhealthy_deployments)
    rest = len(tally.unhealthy_deployments) - UNHEALTHY_NAMES_SHOWN
    return f"{', '.join(tally.unhealthy_deployments[:UNHEALTHY_NAMES_SHOWN])} en {rest} andere"


def _unhealthy_counts(tally: RolloutTally) -> tuple[str, str]:
    """How many deployments are unhealthy, and the verb that agrees with that count.

    A COUNT, not a concatenation: each warning carries the full kubelet text, which for
    fifteen deployments is one row title of thirteen thousand characters. What is wrong
    per component is in ``component_failures``.
    """
    count = len(tally.unhealthy_deployments)
    how_many = (
        f"{count} van de {tally.synced_deployment_count} deployments"
        if tally.synced_deployment_count > count
        else f"{count} deployment" + ("" if count == 1 else "s")
    )
    return how_many, "draait" if count == 1 else "draaien"


def _unhealthy_cause(tally: RolloutTally) -> str:
    """The closing sentence: who has to act on these unhealthy deployments."""
    if tally.registry_outage_seen:
        return (
            "Een deel ligt aan het platform: de registry kon een image niet leveren. "
            "Daar hoef je zelf niets voor te doen, het ophalen wordt vanzelf opnieuw geprobeerd."
        )
    return (
        "Dat ligt aan de applicatie zelf (bijvoorbeeld een crashende pod of een image dat "
        "nog niet gebouwd is), niet aan het uitrollen."
    )
