"""
Deployment health watcher: OOM, ImagePullBackOff, and CrashLoopBackOff detection.

This module OBSERVES (kubectl queries, scheduling, remediation). The judgement -- what
an observation means -- belongs to the ``deployment-health`` system service
(``opi/services/catalog/deployment_health``), which is where the state other services
report about the deployment is weighed in. Same split as resource-tuning: the service is
the declarative home of the decision, this module does the work.

Provides two mechanisms:
1. **Inline detection** (``create_health_check_callback``):
   Used during the ArgoCD polling loop to detect pod health issues while
   the application is still ``Progressing``.  When detected, raises
   ``DeploymentHealthError`` so the caller can handle each failure type.

2. **Fire-and-forget** (``schedule_oom_check``):
   After a deploy or refresh completes, a delayed background check queries
   kubectl for OOM kills and image pull errors.  If detected, queues a
   task for remediation via the task queue (no direct reprocessing).

Failure type handling:
- **OOM**: Auto-tune memory limits and queue a refresh task.
- **ImagePullBackOff**: Queue a task to disable the component (``replicas: 0``).
  Re-enabled when a new image is pushed via ``update_image_and_regenerate()``.
- **CrashLoopBackOff**: Report only, no remediation.  Pods stay running
  so users can access logs.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opi.connectors.kubectl import KubectlConnectionError, KubectlConnector, KubectlExecutionError
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import IMAGE_PULL_REASONS as _IMAGE_PULL_REASONS
from opi.handlers.project_file_handler import is_transient_registry_error
from opi.services.catalog.base import SERVICE_ROLE_LABEL_KEY, application_pod_selector
from opi.services.catalog.deployment_health import deployment_health_service
from opi.services.deployment_state import DeploymentState, collect_deployment_state
from opi.services.resource_tuning_service import get_project_data
from opi.utils.naming import generate_unique_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from opi.core.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)

# Grace period: don't check for pod health issues until the deployment
# has had this many seconds to start up.  Avoids false positives from
# previous OOM kills that haven't been cleared yet by a fresh pod.
HEALTH_CHECK_GRACE_SECONDS = 30

# How often to re-check after the grace period.
# Set to 0 to check every poll iteration (every 5s).
HEALTH_CHECK_INTERVAL_SECONDS = 0

# Stop checking after this many seconds (boot-time failures are fast).
HEALTH_CHECK_MAX_ELAPSED_SECONDS = 120

# When a component's waiting reason stays unchanged for this long, the progress
# UI explicitly flags the stall (and points the user at that component's logs)
# so silence during a stuck rollout becomes an actionable message.
STALL_NOTICE_SECONDS = 45

# Maximum number of OOM → tune → reprocess cycles per deployment.
# With the sliding bump factor (3x/2x/1.5x), 3 attempts covers:
#   25Mi → 75Mi → 150Mi → 300Mi  (should be enough for any boot)
#
# The budget counts ATTEMPTED tune cycles, not realised changes -- weighed and kept
# (RC-160 task D). The two paths therefore charge at different moments, and that
# asymmetry is deliberate rather than an oversight:
#
#   - Fire-and-forget (``_run_oom_check``) charges only after the tune committed
#     something (``observation.requeue_refresh``). It can afford to: when nothing was
#     committed it queues no refresh AND schedules no follow-up check, so the chain
#     ends by itself. The counter is only there to bound it ACROSS rounds.
#   - Inline (``_callback``) charges on detection, before raising
#     ``DeploymentHealthError``. It has no choice: the tune runs afterwards in
#     ``project_manager``, outside the callback, so the callback cannot know the
#     outcome. The counter is its only brake, and a brake that only engages once the
#     work is proven wasted is no brake at all.
#
# The cost of charging early -- a detection blocked by the 8x ceiling spending budget
# without anything being adjusted -- is near-unreachable in practice. A non-committing
# inline detection queues no automated refresh, so a next round can only start from a
# user action (deploy, upsert, manual refresh, image bump), and every one of those
# calls ``reset_oom_tune_attempts`` first. Three such detections in a row therefore
# cannot stack up. And where the budget does close on a ceiling-blocked deployment,
# closing it is the correct outcome: nothing can be adjusted, so the honest answer is
# "manual intervention required" instead of aborting every sync wait for ever.
OOM_MAX_TUNE_ATTEMPTS = 3

# Tracks how many OOM tune cycles have fired per deployment during the current
# process lifetime.  Keyed by "project/deployment".
#
# ONE counter for BOTH paths (inline and fire-and-forget), and it deliberately
# survives a round: every committed tune queues a refresh_deployment task, and that
# task schedules a fresh check. A per-round counter therefore resets the very brake
# it is meant to be (asses-k2n/pr-494, 24 August: 45Mi → 4096Mi in nine steps, each
# round restarting at 1/3). Only an explicit reset -- a real new deploy, a user
# action, an image bump -- clears it; see ``reset_oom_tune_attempts``.
_oom_tune_attempts: dict[str, int] = {}

# The pod-template-hash the last OOM tune acted on, per "project/deployment/component".
# A detection on that same hash is not new evidence: the pod that OOM'd is still the
# one from before the previous increase, so that increase has not rolled out yet.
_last_tuned_pod_template_hash: dict[str, str] = {}

# Module-level task service reference for the fire-and-forget path.
# Set during app startup via ``set_task_service()``.
_task_service_ref: AsyncTaskService | None = None


def set_task_service(task_service: AsyncTaskService) -> None:
    """Store a reference to the task service for fire-and-forget use."""
    global _task_service_ref
    _task_service_ref = task_service


def _oom_attempt_key(project_name: str, deployment_name: str) -> str:
    """The key both paths share for one deployment's OOM tune budget."""
    return f"{project_name}/{deployment_name}"


def oom_tune_budget_spent(project_name: str, deployment_name: str) -> bool:
    """True when this deployment has used up its OOM tune cycles."""
    return _oom_tune_attempts.get(_oom_attempt_key(project_name, deployment_name), 0) >= OOM_MAX_TUNE_ATTEMPTS


def _oom_hash_key(project_name: str, deployment_name: str, component_name: str) -> str:
    return f"{project_name}/{deployment_name}/{component_name}"


def oom_is_fresh_evidence(
    project_name: str,
    deployment_name: str,
    component_name: str,
    pod_template_hash: str | None,
) -> bool:
    """False when this OOM was observed on the same pod generation as the last tune.

    A tune only means something once it is running. During the incident the health
    error broke off the ArgoCD sync wait before the previous increase had rolled out,
    so the watcher kept reading the SAME unchanged pod as fresh evidence: all twelve
    detections came from ``pr-494-api-fb654fcc5-rcf6g``. The existing superseded-
    generation filter could not catch that -- at that moment the pod still WAS the
    current generation.

    An unknown hash (kubectl hiccup, unparsable output) deliberately counts as fresh.
    Blocking there would silence the auto-tune exactly when the cluster is already
    having trouble, which is worse than one tune too many.
    """
    if not pod_template_hash:
        logger.info(
            "Health watcher: no pod-template-hash for %s/%s component %s, treating the OOM as fresh evidence",
            project_name,
            deployment_name,
            component_name,
        )
        return True
    return _last_tuned_pod_template_hash.get(_oom_hash_key(project_name, deployment_name, component_name)) != (
        pod_template_hash
    )


def _record_oom_tune_hash(
    project_name: str,
    deployment_name: str,
    component_name: str,
    pod_template_hash: str | None,
) -> None:
    """Remember which pod generation this tune answered."""
    if pod_template_hash:
        _last_tuned_pod_template_hash[_oom_hash_key(project_name, deployment_name, component_name)] = pod_template_hash


def _record_oom_tune_attempt(project_name: str, deployment_name: str) -> int:
    """Count one OOM tune cycle for this deployment and return the new total.

    Read-modify-write on the shared dict rather than on a snapshot, so two callbacks
    (or a callback and a background check) racing on the same deployment see each
    other's increments.
    """
    key = _oom_attempt_key(project_name, deployment_name)
    _oom_tune_attempts[key] = _oom_tune_attempts.get(key, 0) + 1
    return _oom_tune_attempts[key]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class PodHealthResult:
    """Result of a unified pod health check for one component."""

    component_name: str
    oom_detected: bool = False
    # pod-template-hash of the pod the OOM was observed on; None when it could not be
    # determined. Same hash on a later detection means the previous increase has not
    # rolled out yet, so that detection is not new evidence.
    oom_pod_template_hash: str | None = None
    image_pull_error: str | None = None
    image_pull_container: str | None = None  # which container failed (main "app" vs a sidecar)
    image_pull_image: str | None = None  # the image reference that could not be pulled
    crash_loop_detected: bool = False
    crash_loop_message: str | None = None


@dataclass
class ComponentFailure:
    """One component's failure details."""

    component_name: str  # unique name (deployment-component)
    failure_type: str  # "oom" | "image_pull" | "crash_loop"
    message: str
    deployment_name: str = ""  # user-facing deployment name
    component_reference: str = ""  # user-facing component reference
    logs: list[str] | None = None  # last log lines captured before failure
    container_name: str = ""  # the container that failed: main "app" vs an injected sidecar
    image: str = ""  # the image reference that failed to pull (image_pull only)


class DeploymentHealthError(Exception):
    """Raised when pod health issues are detected during deployment polling."""

    def __init__(self, failures: list[ComponentFailure], namespace: str):
        self.failures = failures
        self.namespace = namespace
        summary = "; ".join(f"{f.component_name}: {f.failure_type}" for f in failures)
        super().__init__(f"Pod health issues in {namespace}: {summary}")


# Container waiting reasons that mean "the image can't be made available, so the
# container will never start". Canonical set lives in project_file_handler
# (IMAGE_PULL_REASONS) so this live detector and the re-enable logic there never
# drift. ErrImageNeverPull happens with imagePullPolicy: Never (e.g. kind/sandbox
# local images that were never side-loaded) — same user-facing outcome as
# ImagePullBackOff, and terminal (it never self-heals), so it counts as image-pull.
_CRASH_LOOP_REASONS = {"CrashLoopBackOff"}

# The component's own container is named "app" in deployment.yaml.jinja; every other
# container in the pod (authorization-wall, db-console, ...) is an injected sidecar.
# Used to distinguish "the user's image failed" from "a platform sidecar image failed",
# which must be reported (and remediated) differently.
MAIN_CONTAINER_NAME = "app"

# Label Kubernetes puts on every ReplicaSet and its pods to identify the pod
# template generation they belong to. Used to evaluate only the current generation.
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"

# Annotation the Deployment controller stamps on each ReplicaSet; the highest
# value is the ReplicaSet the Deployment currently rolls out (also after a
# rollback, which re-stamps the reused ReplicaSet with a new, higher revision).
_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


async def _get_current_pod_template_hash(kubectl: KubectlConnector, namespace: str, unique_name: str) -> str | None:
    """
    Return the ``pod-template-hash`` of the Deployment's current ReplicaSet.

    Lists the ReplicaSets carrying ``app={unique_name}`` and picks the one owned
    by the Deployment with the highest ``deployment.kubernetes.io/revision``.

    Returns None when the hash cannot be determined (no Deployment-owned
    ReplicaSet, kubectl failure, unparsable output). The caller then falls back
    to evaluating every pod.
    """
    try:
        args = ["get", "replicasets", "-n", namespace, "-l", application_pod_selector(unique_name), "-o", "json"]
        stdout, stderr, code = await kubectl.run_command(args)
        if code != 0:
            logger.warning("Failed to list replicasets for %s/%s: %s", namespace, unique_name, stderr)
            return None
        items = json.loads(stdout).get("items", [])
    except (KubectlConnectionError, KubectlExecutionError, json.JSONDecodeError) as e:
        logger.warning("Error listing replicasets for %s/%s: %s", namespace, unique_name, e)
        return None

    best_revision = -1
    best_hash: str | None = None
    for replica_set in items:
        metadata = replica_set.get("metadata", {})
        owners = metadata.get("ownerReferences", [])
        if not any(o.get("kind") == "Deployment" and o.get("name") == unique_name for o in owners):
            continue
        pod_template_hash = metadata.get("labels", {}).get(POD_TEMPLATE_HASH_LABEL)
        if not pod_template_hash:
            continue
        try:
            revision = int(metadata.get("annotations", {}).get(_REVISION_ANNOTATION, ""))
        except ValueError:
            continue
        if revision > best_revision:
            best_revision = revision
            best_hash = pod_template_hash

    return best_hash


async def check_pod_health(namespace: str, unique_name: str) -> PodHealthResult:
    """
    Detect OOM, ImagePullBackOff, and CrashLoopBackOff for one component.

    Runs ``kubectl get pods -o json`` and inspects each container's state for
    all three failure types. Only pods of the Deployment's current generation
    are evaluated (see ``_get_current_pod_template_hash``); pods of a replaced
    ReplicaSet report a problem that no longer exists.

    Failure types:
    - OOM: ``lastState.terminated.reason == "OOMKilled"`` (the cgroup OOM-killer
      signal). A bare ``exitCode == 137`` is NOT treated as OOM: 137 is
      ``128 + SIGKILL`` and is also produced by failed startup/liveness probes,
      node-pressure evictions, and manual kills.
    - ImagePull: ``state.waiting.reason`` in {ImagePullBackOff, ErrImagePull,
      InvalidImageName, ErrImageNeverPull, ImageInspectError, RegistryUnavailable}
    - CrashLoop: ``state.waiting.reason == "CrashLoopBackOff"``

    Args:
        namespace: Kubernetes namespace to search
        unique_name: Deployment/pod name prefix (label selector ``app={unique_name}``)

    Returns:
        PodHealthResult with all detected issues
    """
    result = PodHealthResult(component_name=unique_name)
    kubectl = KubectlConnector()

    if not KubectlConnector.isConnected:
        logger.warning("kubectl not connected, cannot check pod health for %s", unique_name)
        return result

    try:
        # Only the application's own pods: a service running something alongside it
        # (sleep-mode's waker) answers to the same app label, and reading ITS state as
        # the component's reported failures for a component that was not even running.
        args = ["get", "pods", "-n", namespace, "-l", application_pod_selector(unique_name), "-o", "json"]
        stdout, stderr, code = await kubectl.run_command(args)

        if code != 0:
            logger.warning("Failed to get pods for health check (%s/%s): %s", namespace, unique_name, stderr)
            return result

        pods = json.loads(stdout).get("items", [])
        if not pods:
            return result

        # Only the current pod generation says anything about this rollout. Pods of a
        # replaced ReplicaSet keep running (and keep their CrashLoop/OOM/image-pull
        # state) until the controller reaps them, and they carry no deletionTimestamp
        # while doing so, so the check below cannot catch them.
        current_pod_template_hash = await _get_current_pod_template_hash(kubectl, namespace, unique_name)
        if current_pod_template_hash is None:
            logger.warning(
                "Could not determine the current pod-template-hash for %s/%s; "
                "evaluating all pods, so a pod from a replaced ReplicaSet may be reported",
                namespace,
                unique_name,
            )

        for pod in pods:
            metadata = pod.get("metadata", {})
            pod_name = metadata.get("name", "unknown")
            pod_created = metadata.get("creationTimestamp", "")

            # Skip pods that are being replaced (terminating). During a rollout the old
            # ReplicaSet's pods linger with a stale lastState (e.g. an OOM from an earlier
            # lifecycle) while the new pods are healthy. Reading them produces a phantom
            # OOM/CrashLoop that fails the deploy for a problem that no longer exists.
            if metadata.get("deletionTimestamp"):
                logger.debug("Skipping terminating pod %s for health check in %s", pod_name, namespace)
                continue

            # Skip pods of a superseded generation (they outlive their ReplicaSet's
            # replacement without ever getting a deletionTimestamp).
            pod_template_hash = metadata.get("labels", {}).get(POD_TEMPLATE_HASH_LABEL, "")
            if current_pod_template_hash is not None and pod_template_hash != current_pod_template_hash:
                logger.debug(
                    "Skipping pod %s from superseded generation %s (current %s) in %s",
                    pod_name,
                    pod_template_hash or "unknown",
                    current_pod_template_hash,
                    namespace,
                )
                continue

            for container_status in pod.get("status", {}).get("containerStatuses", []):
                container_name = container_status.get("name", "unknown")

                # Check OOM via lastState.terminated
                last_state = container_status.get("lastState", {})
                terminated = last_state.get("terminated", {})
                reason = terminated.get("reason", "")
                exit_code = terminated.get("exitCode")
                if reason == "OOMKilled":
                    oom_finished = terminated.get("finishedAt", "")
                    if pod_created and oom_finished and oom_finished < pod_created:
                        logger.debug(
                            "Ignoring stale OOM for pod %s (oom=%s < created=%s)",
                            pod_name,
                            oom_finished,
                            pod_created,
                        )
                    else:
                        logger.info(
                            "OOM kill detected for pod %s container %s in %s (reason=%s, exitCode=%s)",
                            pod_name,
                            container_name,
                            namespace,
                            reason,
                            exit_code,
                        )
                        result.oom_detected = True
                        result.oom_pod_template_hash = pod_template_hash or current_pod_template_hash

                # Check waiting state for ImagePull and CrashLoop
                waiting = container_status.get("state", {}).get("waiting", {})
                waiting_reason = waiting.get("reason", "")

                if waiting_reason in _IMAGE_PULL_REASONS:
                    message = waiting.get("message", "image pull failed")
                    image = container_status.get("image", "")
                    logger.info(
                        "Image pull error for pod %s container %s (image %s) in %s: %s - %s",
                        pod_name,
                        container_name,
                        image,
                        namespace,
                        waiting_reason,
                        message,
                    )
                    # A pod can have several containers; the main "app" container is the
                    # user's own image and takes precedence. Never let a sidecar's failure
                    # overwrite (or masquerade as) the main container's.
                    if result.image_pull_error is None or container_name == MAIN_CONTAINER_NAME:
                        result.image_pull_error = f"{waiting_reason}: {message}"
                        result.image_pull_container = container_name
                        result.image_pull_image = image

                if waiting_reason in _CRASH_LOOP_REASONS:
                    message = waiting.get("message", "container keeps crashing")
                    logger.info(
                        "CrashLoopBackOff for pod %s container %s in %s: %s",
                        pod_name,
                        container_name,
                        namespace,
                        message,
                    )
                    result.crash_loop_detected = True
                    result.crash_loop_message = f"CrashLoopBackOff: {message}"

    except Exception as e:
        logger.warning("Error checking pod health for %s/%s: %s", namespace, unique_name, e)

    return result


# De kubelet-message hoort op EEN regel te passen: wat deze functie teruggeeft komt in de
# voortgangslijst terecht als de titel van een lijstitem, niet als lopende tekst. Gemeten
# op productie is een image-pull-message van CRI-O 762 tekens, omdat dezelfde fout er twee
# keer in staat (eerst als ``pull image err``, daarna als ``artifact err``). Die kwam
# integraal in die titel terecht, maal twee componenten maal vijftien deployments.
#
# Vandaar deze grens. Het volledige bericht verdwijnt niet: dat blijft staan in
# ``component_failures`` (met een vertaalde titel en een suggestie) en in de logs.
_MAX_WAITING_DETAIL = 120


def _short_detail(message: str) -> str:
    """Vouw een kubelet-message op tot iets dat als regeltitel leesbaar blijft."""
    collapsed = " ".join(message.split())
    if len(collapsed) <= _MAX_WAITING_DETAIL:
        return collapsed
    return collapsed[: _MAX_WAITING_DETAIL - 1].rstrip() + "\u2026"


def _describe_pod_waiting(pod: dict) -> str | None:
    """Return a plain-language reason a pod is not Ready yet, or None if it looks ready.

    Keeps the Kubernetes reason (something to search for) and a shortened form of the
    message, wrapped in Dutch framing for the common cases.
    """
    status = pod.get("status", {})
    phase = status.get("phase", "")
    container_statuses = status.get("containerStatuses", [])

    # Not yet scheduled onto a node (e.g. insufficient memory/cpu, no node).
    if phase == "Pending":
        for cond in status.get("conditions", []):
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                msg = cond.get("message") or cond.get("reason") or "geen geschikte node beschikbaar"
                return f"kan niet worden ingepland: {_short_detail(msg)}"

    # Container-level waiting reasons (the Kubernetes reason, plus a shortened message).
    for cs in container_statuses:
        waiting = cs.get("state", {}).get("waiting")
        if waiting:
            reason = waiting.get("reason", "")
            message = waiting.get("message", "")
            suffix = f": {_short_detail(message)}" if message else ""
            if reason in _IMAGE_PULL_REASONS:
                # Bewust helemaal zonder message, ook niet ingekort: hier is dat altijd
                # de registry-dump uit de toelichting bij _MAX_WAITING_DETAIL, en de eerste
                # 120 tekens daarvan zeggen niets wat de reden hierboven niet al zegt. Welk
                # image het is en wat eraan te doen valt staat in component_failures.
                return f"image ophalen mislukt ({reason})"
            if reason in _CRASH_LOOP_REASONS:
                return f"blijft herstarten na een crash{suffix}"
            if reason == "ContainerCreating":
                return f"container wordt aangemaakt{suffix}"
            if reason:
                return f"{reason}{suffix}"

    # Running but not passing its readiness check (the classic silent stall).
    not_ready = [
        cs.get("name", "?")
        for cs in container_statuses
        if "running" in cs.get("state", {}) and not cs.get("ready", False)
    ]
    if not_ready:
        return "draait, maar is nog niet gereed (readiness-check nog niet geslaagd)"

    # Pod accepted but containers not reported yet.
    if not container_statuses:
        return "bezig met opstarten"

    return None


async def describe_components_waiting(
    namespace: str,
    component_names: list[str],
    component_refs: dict[str, str] | None = None,
    state: DeploymentState | None = None,
) -> list[tuple[str, str]]:
    """Describe, in plain language, why each component is not ready yet.

    Diagnostic counterpart to :func:`check_pod_health`: it never raises and
    never remediates. A single kubectl call lists the namespace pods; for every
    component whose representative pod is not yet Ready it returns a
    human-readable reason (scheduling problem, image pull, crash loop, container
    creating, readiness not passing, ...).

    Two things make this honest about a deployment another service acted on:

    * pods a service runs alongside the application are skipped. They carry the
      component's ``app`` label on purpose (sleep-mode's waker takes over the
      component's Service), so matching on that label alone reported the WAKER's
      ``ImagePullBackOff`` as the component's reason -- the exact message the
      original report was about, from this function.
    * a component with no application pods is explained by whichever service says it
      scaled the application to zero (``state``). Without such a claim the silence is
      still reported, so a deployment that is simply not coming up stays visible.

    Returns a list of ``(component_reference, reason)`` for not-ready components
    only; ready components are omitted.
    """
    refs = component_refs or {}
    wanted = set(component_names)
    kubectl = KubectlConnector()

    if not KubectlConnector.isConnected or not wanted:
        return []

    try:
        args = ["get", "pods", "-n", namespace, "-o", "json"]
        stdout, stderr, code = await kubectl.run_command(args)
        if code != 0:
            logger.debug("describe_components_waiting: kubectl failed for %s: %s", namespace, stderr)
            return []
        pods_data = json.loads(stdout)
    except Exception as e:
        logger.debug("describe_components_waiting: error for %s: %s", namespace, e)
        return []

    # One representative pod per component, matched by the `app` label -- and only the
    # application's own pods: a pod carrying a service role is another service's
    # workload, not this component (see SERVICE_ROLE_LABEL_KEY).
    pod_by_component: dict[str, dict] = {}
    for pod in pods_data.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        if SERVICE_ROLE_LABEL_KEY in labels:
            continue
        app = labels.get("app", "")
        if app in wanted and app not in pod_by_component:
            pod_by_component[app] = pod

    absent_pods_reason = deployment_health_service().absent_pods_are_expected(state or DeploymentState())

    results: list[tuple[str, str]] = []
    for unique_name in component_names:
        ref = refs.get(unique_name, unique_name)
        pod = pod_by_component.get(unique_name)
        if pod is None:
            results.append((ref, absent_pods_reason or "pods worden aangemaakt"))
            continue
        reason = _describe_pod_waiting(pod)
        if reason:
            results.append((ref, reason))
    return results


async def disable_components_for_image_pull(
    project_name: str,
    deployment_name: str,
    disabled_components: list[tuple[str, str]],
) -> None:
    """
    Disable components with image pull errors: update YAML and commit.

    Does NOT trigger reprocessing — the caller is responsible for that
    (typically by queuing a refresh task through the task queue).

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment
        disabled_components: List of (component_reference, error_message) tuples
    """
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.manager.project_manager import ProjectManager
    from opi.services.resource_tuning_service import get_project_data_from_git

    project_data, filename = await get_project_data_from_git(project_name)
    # No connector is threaded in: ProjectManager takes the warm one from the store
    # itself, so no caller can hold -- or close -- it.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")
    try:
        file_handler = ProjectFileHandler()
        names = []
        for component_ref, error_message in disabled_components:
            file_handler.set_deployment_component_disabled(
                project_data, deployment_name, component_ref, True, error_message
            )
            names.append(component_ref)

        commit_msg = f"auto-disable: image pull errors for {', '.join(names)} in {project_name}/{deployment_name}"
        await project_manager.save_and_commit_project(project_data, commit_msg, enforce_validation=False)
    finally:
        await project_manager.close()

    logger.info(
        "Disabled %d component(s) with image pull errors in %s/%s: %s",
        len(disabled_components),
        project_name,
        deployment_name,
        ", ".join(n for n, _ in disabled_components),
    )


async def _run_oom_check(
    project_name: str,
    deployment_name: str,
    attempt: int,
    max_attempts: int,
    delay_seconds: int,
) -> None:
    """
    Internal coroutine: wait, check pod health, remediate if needed.

    Uses the task queue for reprocessing to avoid race conditions with
    concurrent tasks operating on the same deployment.
    """
    await asyncio.sleep(delay_seconds)

    logger.info(
        "Health watcher check starting for %s/%s (attempt %d/%d)",
        project_name,
        deployment_name,
        attempt,
        max_attempts,
    )

    try:
        project_data, _ = get_project_data(project_name)
    except ValueError as e:
        logger.warning("Health watcher: project lookup failed for %s: %s", project_name, e)
        return

    # Find the deployment in project data
    deployments = project_data.get("deployments", [])
    target_dep = None
    for dep in deployments:
        if dep.get("name") == deployment_name:
            target_dep = dep
            break

    if not target_dep:
        logger.warning("Health watcher: deployment '%s' not found in project '%s'", deployment_name, project_name)
        return

    base_namespace = target_dep.get("namespace")
    cluster = target_dep.get("cluster")
    if not base_namespace or not cluster:
        logger.warning("Health watcher: deployment '%s' missing namespace or cluster", deployment_name)
        return

    namespace = get_prefixed_namespace(cluster, base_namespace)

    # What the services report about this deployment. It is weighed by the judgement
    # below, which never lets it excuse an observed problem -- the point of collecting it
    # here is that the remediation (disabling a component on an image-pull failure) is the
    # most destructive thing this module does, so it must run on a complete picture.
    state = collect_deployment_state(project_data, deployment_name)
    if state.facts:
        logger.info(
            "Health watcher: services report for %s/%s: %s",
            project_name,
            deployment_name,
            "; ".join(state.summaries),
        )

    # Check each component for health issues (unified check); the deployment-health
    # service decides what an observation means.
    health_service = deployment_health_service()
    oom_component_refs: list[str] = []
    oom_pod_hashes: dict[str, str | None] = {}  # unique_name -> the generation that OOM'd
    image_pull_errors: list[tuple[str, str]] = []  # (component_ref, error_message)
    components = target_dep.get("components", [])
    for comp in components:
        component_ref = comp.get("reference", "")
        if not component_ref:
            continue
        if comp.get("disabled"):
            continue

        unique_name = generate_unique_name(deployment_name, component_ref)
        health = await check_pod_health(namespace, unique_name)
        if not health_service.counts_as_failure(health, state):
            continue

        if health.oom_detected:
            if oom_is_fresh_evidence(project_name, deployment_name, unique_name, health.oom_pod_template_hash):
                oom_component_refs.append(component_ref)
                oom_pod_hashes[unique_name] = health.oom_pod_template_hash
            else:
                logger.info(
                    "Health watcher: OOM for %s/%s component %s is on pod generation %s, "
                    "the same one the previous tune answered — waiting for that increase to roll out",
                    project_name,
                    deployment_name,
                    component_ref,
                    health.oom_pod_template_hash,
                )
        if health.image_pull_error:
            if is_transient_registry_error(health.image_pull_error):
                logger.warning(
                    "Health watcher: registry failure (not the image) for %s/%s component %s, "
                    "leaving it enabled so kubelet retries the pull: %s",
                    project_name,
                    deployment_name,
                    component_ref,
                    health.image_pull_error,
                )
            else:
                image_pull_errors.append((component_ref, health.image_pull_error))
        # CrashLoopBackOff: no remediation in fire-and-forget — only reported inline

    # Handle image pull errors: disable in YAML, then queue refresh task
    if image_pull_errors:
        try:
            await disable_components_for_image_pull(project_name, deployment_name, image_pull_errors)
            # Queue a refresh task instead of direct reprocessing
            await _queue_refresh_task(project_name, deployment_name)
        except Exception as e:
            logger.error("Failed to handle image pull errors in %s/%s: %s", project_name, deployment_name, e)

    # Handle OOM kills: tune resources (git-only), then queue refresh
    if not oom_component_refs:
        if not image_pull_errors:
            logger.info(
                "Health watcher: no issues detected for %s/%s (attempt %d/%d)",
                project_name,
                deployment_name,
                attempt,
                max_attempts,
            )
        return

    # The shared budget decides, not the ``attempt`` parameter. That parameter only
    # counts within one chain of scheduled checks, and every committed tune queues a
    # refresh whose handler starts a brand new chain at attempt=1 -- so it reset the
    # brake it was supposed to be. ``attempt`` stays in the log lines only.
    if oom_tune_budget_spent(project_name, deployment_name):
        logger.warning(
            "Health watcher: OOM tune budget (%d cycles) spent for %s/%s, no further auto-tune "
            "(attempt %d/%d) — manual intervention required",
            OOM_MAX_TUNE_ATTEMPTS,
            project_name,
            deployment_name,
            attempt,
            max_attempts,
        )
        return

    logger.info(
        "Health watcher: OOM detected for %s/%s, triggering auto-tune (attempt %d/%d)",
        project_name,
        deployment_name,
        attempt,
        max_attempts,
    )

    # Route through the same after-sync hook scan the inline deploy path uses, so the
    # OOM remediation is not hardcoded here either. The runner commits once.
    from opi.services.catalog.base import ComponentHealth
    from opi.services.deployment_observation import run_after_sync_observation

    component_health = {ref: ComponentHealth(oom_detected=True) for ref in oom_component_refs}
    try:
        observation = await run_after_sync_observation(project_name, deployment_name, component_health)
        if observation.requeue_refresh:
            used = _record_oom_tune_attempt(project_name, deployment_name)
            for unique_name, pod_hash in oom_pod_hashes.items():
                _record_oom_tune_hash(project_name, deployment_name, unique_name, pod_hash)
            logger.info(
                "Health watcher: auto-tune committed changes for %s/%s (%d/%d OOM tune cycles used)",
                project_name,
                deployment_name,
                used,
                OOM_MAX_TUNE_ATTEMPTS,
            )
            await _queue_refresh_task(project_name, deployment_name)
            schedule_oom_check(
                project_name,
                deployment_name,
                attempt=attempt + 1,
                max_attempts=max_attempts,
            )
        else:
            logger.info("Health watcher: tune found no actionable changes for %s/%s", project_name, deployment_name)
        for msg in observation.failures:
            logger.warning("Health watcher: %s", msg)
    except Exception as e:
        logger.error("Health watcher: auto-tune failed for %s/%s: %s", project_name, deployment_name, e)


async def _queue_refresh_task(project_name: str, deployment_name: str) -> None:
    """Queue a refresh_deployment task via the task queue.

    Uses the module-level ``_task_service_ref`` set by ``set_task_service()``.
    """
    if _task_service_ref is None:
        logger.warning("Task service not available, cannot queue refresh for %s/%s", project_name, deployment_name)
        return

    await _task_service_ref.create_task(
        task_type="refresh_deployment",
        project_name=project_name,
        deployment_name=deployment_name,
        cluster=settings.CLUSTER_MANAGER,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            "force_clone": False,
            # Automated retry after a disable: must not re-enable moving-tag disables.
            "automated_remediation": True,
        },
    )
    logger.info("Queued refresh task for %s/%s", project_name, deployment_name)


def schedule_oom_check(
    project_name: str,
    deployment_name: str,
    delay_seconds: int | None = None,
    attempt: int = 1,
    max_attempts: int | None = None,
) -> asyncio.Task | None:
    """
    Schedule a delayed health check as a fire-and-forget background task.

    After ``delay_seconds``, queries kubectl for OOM kills and image pull
    errors.  Remediates via the task queue (no direct reprocessing).

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment to monitor
        delay_seconds: Seconds to wait before checking (default from settings)
        attempt: Current attempt number (1-based)
        max_attempts: Maximum tune cycles (default from settings)

    Returns:
        The created asyncio.Task, or None if watcher is disabled or max attempts reached
    """
    if not settings.OOM_WATCHER_ENABLED:
        return None

    if delay_seconds is None:
        delay_seconds = settings.OOM_WATCHER_DELAY_SECONDS
    if max_attempts is None:
        max_attempts = settings.OOM_WATCHER_MAX_ATTEMPTS

    if attempt > max_attempts:
        logger.warning(
            "Health watcher: max attempts (%d) reached for %s/%s, manual intervention required",
            max_attempts,
            project_name,
            deployment_name,
        )
        return None

    logger.info(
        "Health watcher: scheduled check for %s/%s in %ds (attempt %d/%d)",
        project_name,
        deployment_name,
        delay_seconds,
        attempt,
        max_attempts,
    )

    task = asyncio.create_task(
        _run_oom_check(project_name, deployment_name, attempt, max_attempts, delay_seconds),
        name=f"health-watch-{project_name}-{deployment_name}-{attempt}",
    )
    return task


async def check_all_components_health(
    namespace: str,
    component_names: list[str],
    state: DeploymentState | None = None,
) -> list[PodHealthResult]:
    """
    Check multiple components for health issues via kubectl.

    What counts as an issue is the ``deployment-health`` service's call, not this
    module's: it is asked per component, with the state the other services report about
    the deployment. It answers the same way for every observed problem today -- a problem
    on an application pod is a failure, whatever any service says -- and that is the
    point of routing through it: the state is available at the decision and deliberately
    gets no vote.

    Args:
        namespace: Kubernetes namespace
        component_names: List of unique component names (deployment prefixes)
        state: What the services report about this deployment; empty when unknown

    Returns:
        List of PodHealthResult for components that have issues
    """
    deployment_state = state if state is not None else DeploymentState()
    health_service = deployment_health_service()
    results: list[PodHealthResult] = []
    for name in component_names:
        health = await check_pod_health(namespace, name)
        if health_service.counts_as_failure(health, deployment_state):
            results.append(health)
    return results


def create_health_check_callback(
    project_name: str,
    deployment_name: str,
    namespace: str,
    component_names: list[str],
    component_refs: dict[str, str] | None = None,
    grace_seconds: int = HEALTH_CHECK_GRACE_SECONDS,
    state: DeploymentState | None = None,
) -> Callable[[int], Awaitable[None]] | None:
    """
    Build an ``on_progressing`` callback for ``wait_for_application_synced``.

    The callback checks for OOM, ImagePullBackOff, and CrashLoopBackOff
    via kubectl after the grace period.  When any issue is detected,
    raises ``DeploymentHealthError`` with per-component failure details
    including user-facing names and captured logs.

    Args:
        project_name: Project name (for OOM attempt tracking)
        deployment_name: Deployment name (user-facing, for OOM attempt tracking)
        namespace: Kubernetes namespace for the deployment
        component_names: Unique names of the deployment's components
        component_refs: Mapping from unique name to component reference
            (user-facing name). If None, unique names are used as-is.
        grace_seconds: Seconds to wait before checking (default 30)
        state: What the services report about this deployment (RC-28). Passed to the
            judgement, which weighs it; an observed problem is a failure regardless.

    Returns:
        Async callback ``(elapsed_seconds) -> None``. Always non-None: even when
        the OOM auto-tune budget is exhausted, the callback still detects
        ImagePullBackOff and CrashLoopBackOff and raises ``DeploymentHealthError``.
    """
    attempt_key = f"{project_name}/{deployment_name}"
    last_check_at = 0
    exhaustion_logged = False
    stale_generation_logged: set[str] = set()

    async def _callback(elapsed_seconds: int) -> None:
        nonlocal last_check_at, exhaustion_logged

        # Stop checking after max elapsed (boot-time failures are fast)
        if elapsed_seconds > HEALTH_CHECK_MAX_ELAPSED_SECONDS:
            return

        # Throttle checks
        if last_check_at > 0 and (elapsed_seconds - last_check_at) < HEALTH_CHECK_INTERVAL_SECONDS:
            return

        # Read the budget LIVE, on every call. Snapshotting it while building the
        # callback meant it could never flip from "room left" to "spent" inside a
        # callback's lifetime, and two callbacks alive on the same deployment each
        # counted from their own zero. When the budget is spent, only the OOM branch
        # is suppressed -- image-pull and crash-loop detection must keep working,
        # otherwise a broken image on an OOM-exhausted deployment sits in Progressing
        # until ArgoCD's progress deadline. Never return None here.
        current_attempts = _oom_tune_attempts.get(attempt_key, 0)
        oom_budget_exhausted = current_attempts >= OOM_MAX_TUNE_ATTEMPTS
        if oom_budget_exhausted and not exhaustion_logged:
            exhaustion_logged = True
            logger.warning(
                "Health check: max OOM tune attempts (%d) reached for %s, "
                "OOM auto-tune disabled (image-pull/crash-loop still checked)",
                OOM_MAX_TUNE_ATTEMPTS,
                attempt_key,
            )

        # CrashLoopBackOff and ImagePullBackOff are visible immediately —
        # no grace period needed.  OOM needs the grace period because
        # lastState.terminated can contain stale data from a previous pod.
        # Once the OOM auto-tune budget is exhausted, stop treating OOM as a
        # detectable failure (no more tune cycles) but keep checking the rest.
        check_oom = elapsed_seconds >= grace_seconds and not oom_budget_exhausted

        is_first_check = last_check_at == 0
        last_check_at = elapsed_seconds
        log = logger.info if is_first_check else logger.debug
        log(
            "Health check: probing %d component(s) in %s (elapsed %ds, oom=%s, %d/%d OOM tune cycles used)",
            len(component_names),
            namespace,
            elapsed_seconds,
            check_oom,
            current_attempts,
            OOM_MAX_TUNE_ATTEMPTS,
        )

        unhealthy = await check_all_components_health(namespace, component_names, state)
        if not unhealthy:
            logger.info("Health check: no issues detected in %s", namespace)
            return

        # Build per-component failure list with friendly names and logs
        refs = component_refs or {}
        kubectl = KubectlConnector()
        failures: list[ComponentFailure] = []
        has_oom = False
        for health in unhealthy:
            comp_ref = refs.get(health.component_name, health.component_name)

            # Capture logs for actionable diagnostics
            logs: list[str] | None = None
            if health.crash_loop_detected or health.oom_detected:
                try:
                    logs = await kubectl.get_deployment_logs(health.component_name, namespace, lines=20)
                except Exception as log_err:
                    logger.debug("Failed to capture logs for %s: %s", health.component_name, log_err)

            # The grace period guards against stale lastState from a prior pod. That
            # concern doesn't apply when the container is actively crash-looping now —
            # the OOM is guaranteed to be from the current lifecycle. Without this
            # exception, pods that OOM instantly on boot (e.g. 25Mi limit) get reported
            # only as CrashLoopBackOff and the auto-tune path never runs.
            oom_actionable = (
                not oom_budget_exhausted and health.oom_detected and (check_oom or health.crash_loop_detected)
            )
            # Only ask about the generation for an OOM we would otherwise act on, and
            # say so once per component: this runs on every poll iteration.
            if oom_actionable and not oom_is_fresh_evidence(
                project_name, deployment_name, health.component_name, health.oom_pod_template_hash
            ):
                oom_actionable = False
                if health.component_name not in stale_generation_logged:
                    stale_generation_logged.add(health.component_name)
                    logger.info(
                        "Health check: OOM for %s is on pod generation %s, the same one the previous tune "
                        "answered — waiting for that increase to roll out",
                        health.component_name,
                        health.oom_pod_template_hash,
                    )
            if oom_actionable:
                has_oom = True
                _record_oom_tune_hash(
                    project_name, deployment_name, health.component_name, health.oom_pod_template_hash
                )
                failures.append(
                    ComponentFailure(
                        component_name=health.component_name,
                        failure_type="oom",
                        message="OOM kill detected",
                        deployment_name=deployment_name,
                        component_reference=comp_ref,
                        logs=logs,
                    )
                )
            if health.image_pull_error:
                failures.append(
                    ComponentFailure(
                        component_name=health.component_name,
                        failure_type="image_pull",
                        message=health.image_pull_error,
                        deployment_name=deployment_name,
                        component_reference=comp_ref,
                        container_name=health.image_pull_container or "",
                        image=health.image_pull_image or "",
                    )
                )
            if health.crash_loop_detected:
                failures.append(
                    ComponentFailure(
                        component_name=health.component_name,
                        failure_type="crash_loop",
                        message=health.crash_loop_message or "CrashLoopBackOff",
                        deployment_name=deployment_name,
                        component_reference=comp_ref,
                        logs=logs,
                    )
                )

        if not failures:
            # Only OOM detected but still in grace period — skip for now
            return

        if has_oom:
            _record_oom_tune_attempt(project_name, deployment_name)

        raise DeploymentHealthError(failures, namespace)

    # Deliberately NO reset here. Building a callback is not proof of a fresh deploy:
    # the automated refresh queued by a tune builds one too, so popping the counter on
    # creation wiped the budget once per escalation round. Only an explicit reset
    # (``reset_oom_tune_attempts``, called for a real deploy / user action / image
    # bump) clears it.
    return _callback


def reset_oom_tune_attempts(project_name: str, deployment_name: str) -> None:
    """Clear a deployment's OOM tune budget: a real new deploy starts clean.

    Called for user-initiated work only (a deploy, an upsert, a manual refresh, an
    image bump) — never for the automated refresh a tune queues for itself, which
    carries ``automated_remediation: True`` precisely so it can be told apart.
    """
    _oom_tune_attempts.pop(_oom_attempt_key(project_name, deployment_name), None)
    prefix = f"{project_name}/{deployment_name}/"
    for key in [k for k in _last_tuned_pod_template_hash if k.startswith(prefix)]:
        del _last_tuned_pod_template_hash[key]
