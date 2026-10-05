"""
Deployment health watcher: OOM, ImagePullBackOff, and CrashLoopBackOff detection.

This module OBSERVES (kubectl queries, scheduling, remediation). The judgement -- what
an observation means -- belongs to the ``deployment-health`` system service
(``opi/services/catalog/deployment_health``), which is where the state other services
report about the deployment is weighed in. Same split as resource-tuning: the service is
the declarative home of the decision, this module does the work.

Provides three mechanisms:
1. **Inline detection** (``create_health_check_callback``):
   Used during the ArgoCD polling loop to detect pod health issues while
   the application is still ``Progressing``.  When detected, raises
   ``DeploymentHealthError`` so the caller can handle each failure type.

2. **Fire-and-forget** (``schedule_oom_check``):
   After a deploy or refresh completes, a delayed background check queries
   kubectl for OOM kills and image pull errors.  An OOM queues a task for
   remediation via the task queue (no direct reprocessing).

3. **Cluster-wide pod watch** (``OomPodWatcher``, ``OomMetricSweeper``):
   Streams every application pod on this cluster, so an OOM on a pod that has been
   running for days is caught too; the metric sweep is the net under it. Both
   remediate through ``apply_oom_tune``, and stay off unless the cluster config
   says so (``watches_pods_for_oom``).

Failure type handling:
- **OOM**: Auto-tune memory limits and queue a refresh task.
- **ImagePullBackOff**: Report only, no remediation (RC-243). Kubelet retries the pull
  with its own backoff, so the pod that recovers has to stay; the component is named on
  the deployment card for as long as it fails.
- **CrashLoopBackOff**: Report only, no remediation.  Pods stay running
  so users can access logs.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opi.connectors.kubectl import KubectlConnectionError, KubectlConnector, KubectlExecutionError
from opi.connectors.prometheus import get_metrics_connector
from opi.core.cluster_config import get_namespace_prefix, get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import IMAGE_PULL_REASONS as _IMAGE_PULL_REASONS
from opi.handlers.project_file_handler import classify_image_pull_failure
from opi.services.catalog.base import (
    SERVICE_ROLE_LABEL_KEY,
    all_application_pods_selector,
    application_pod_selector,
    is_application_pod,
)
from opi.services.catalog.deployment_health import deployment_health_service
from opi.services.deployment_state import DeploymentState, collect_deployment_state
from opi.services.resource_tuning_service import get_project_data
from opi.utils.naming import generate_unique_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

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


async def apply_oom_tune(
    project_name: str,
    deployment_name: str,
    component_refs: list[str],
    pod_hashes: dict[str, str | None],
    source: str,
) -> bool:
    """Tune the memory limits for observed OOM kills and queue the refresh.

    The single remediation path: the fire-and-forget check after a deploy and the
    cluster-wide pod watch both end here, so the budget, the pod-generation lock and
    the tune itself cannot drift apart between them.

    ``pod_hashes`` maps each component unique name to the pod generation the OOM was seen
    on; ``source`` only reaches the log lines. Returns True when the tune committed a
    change, and a refresh was queued for it.
    """
    if oom_tune_budget_spent(project_name, deployment_name):
        logger.warning(
            "Health watcher: OOM tune budget (%d cycles) spent for %s/%s, no further auto-tune "
            "via %s, manual intervention required",
            OOM_MAX_TUNE_ATTEMPTS,
            project_name,
            deployment_name,
            source,
        )
        return False

    logger.info(
        "Health watcher: OOM detected for %s/%s via %s, triggering auto-tune",
        project_name,
        deployment_name,
        source,
    )

    # Route through the same after-sync hook scan the inline deploy path uses, so the
    # OOM remediation is not hardcoded here either. The runner commits once.
    from opi.services.catalog.base import ComponentHealth
    from opi.services.deployment_observation import run_after_sync_observation

    component_health = {ref: ComponentHealth(oom_detected=True) for ref in component_refs}
    try:
        observation = await run_after_sync_observation(project_name, deployment_name, component_health)
        committed = observation.requeue_refresh
        if committed:
            used = _record_oom_tune_attempt(project_name, deployment_name)
            for unique_name, pod_hash in pod_hashes.items():
                _record_oom_tune_hash(project_name, deployment_name, unique_name, pod_hash)
            logger.info(
                "Health watcher: auto-tune committed changes for %s/%s (%d/%d OOM tune cycles used)",
                project_name,
                deployment_name,
                used,
                OOM_MAX_TUNE_ATTEMPTS,
            )
            await _queue_refresh_task(project_name, deployment_name)
        else:
            logger.info("Health watcher: tune found no actionable changes for %s/%s", project_name, deployment_name)
        for msg in observation.failures:
            logger.warning("Health watcher: %s", msg)
        return committed
    except Exception as e:
        logger.error("Health watcher: auto-tune failed for %s/%s: %s", project_name, deployment_name, e)
        return False


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
    # here is that the remediation (raising a memory limit on an OOM kill) writes to the
    # project file, so it must run on a complete picture.
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
    image_pull_refs: list[str] = []  # components that cannot pull, reported only
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
            # Reported, never remediated (RC-243). The classification still picks the
            # wording; what it no longer does is decide that a component goes to zero
            # replicas, which took away the pod that was retrying the pull. Kubelet keeps
            # retrying with its own backoff, and the component is named on the deployment
            # card for as long as it fails (see features/image-pull-backoff-detection.md).
            image_pull_refs.append(component_ref)
            logger.warning(
                "Health watcher: %s/%s component %s cannot pull its image (%s), leaving it "
                "enabled so kubelet retries the pull: %s",
                project_name,
                deployment_name,
                component_ref,
                classify_image_pull_failure(health.image_pull_error),
                health.image_pull_error,
            )
        # CrashLoopBackOff: no remediation in fire-and-forget — only reported inline

    # Handle OOM kills: tune resources (git-only), then queue refresh
    if not oom_component_refs:
        if not image_pull_refs:
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
    committed = await apply_oom_tune(
        project_name,
        deployment_name,
        oom_component_refs,
        oom_pod_hashes,
        source=f"fire-and-forget check (attempt {attempt}/{max_attempts})",
    )
    if committed:
        schedule_oom_check(
            project_name,
            deployment_name,
            attempt=attempt + 1,
            max_attempts=max_attempts,
        )


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


# ---------------------------------------------------------------------------
# Cluster-wide pod watch: OOM kills outside the deploy window
# ---------------------------------------------------------------------------
#
# Kubernetes has no Event for an OOM kill; the information sits in the pod status, in
# ``containerStatuses[].lastState.terminated``. That is why this watch reads pods and not
# events. Why it exists, and the measurements behind it: features/oom-pod-watch.md

# How long to wait before relisting after the stream ends, and the ceiling the backoff
# climbs to when the stream keeps failing immediately (RBAC gone, API server down).
POD_WATCH_RECONNECT_SECONDS = 5
POD_WATCH_MAX_RECONNECT_SECONDS = 120

# A stream that delivered nothing is a stream that failed; only one that actually ran
# resets the backoff.
_POD_WATCH_HEALTHY_EVENTS = 1

# Kills already handled, keyed by pod+container+``finishedAt``. The kill is unique in
# time, so this survives a relist (kubectl replays every pod as ADDED) and an OPI restart
# in the middle of a crash loop.
#
# Recorded once the kill is READ, not once it is tuned: a kill we decided not to act on
# must not be re-evaluated, and a failed tune is not retried on the same evidence.
_MAX_HANDLED_OOM_KILLS = 2000
_handled_oom_kills: dict[str, None] = {}

# ``kube_pod_container_status_last_terminated_reason`` is the only metric that exposes an
# OOM kill, and it is EXPERIMENTAL in kube-state-metrics, so it can disappear under us.
# That is why it is the net and never the detection: see ``read_pod_oom_kill``.
#
# A bare selector, not the ``max_over_time`` of the sister query in
# ``resource_tuning_service``: the reason for that range is that the metric only exists
# while the stopped pod does (``tests/test_oom_query_venster.py``), and here a range would
# only add rows that ``read_pod_oom_kill`` throws away again, because it verifies every hit
# against a live pod. The boundary that comes with that is in features/oom-pod-watch.md:
# this net only sees kills whose pod still exists.
_OOM_METRIC_QUERY = 'kube_pod_container_status_last_terminated_reason{{reason="OOMKilled", namespace=~"{prefix}.*"}}'


@dataclass
class ObservedOomKill:
    """One OOM kill read off a pod, before it is resolved to a component."""

    namespace: str
    pod_name: str
    container_name: str
    finished_at: str
    project_name: str
    deployment_name: str
    app_name: str
    pod_template_hash: str | None


def _restart_key(namespace: str, pod_name: str, container_name: str) -> str:
    return f"{namespace}/{pod_name}/{container_name}"


def _oom_kill_key(kill: ObservedOomKill) -> str:
    return f"{kill.namespace}/{kill.pod_name}/{kill.container_name}/{kill.finished_at}"


def _claim_oom_kill(kill: ObservedOomKill) -> bool:
    """True the first time this exact kill is seen, False on every repeat."""
    key = _oom_kill_key(kill)
    if key in _handled_oom_kills:
        return False
    _handled_oom_kills[key] = None
    while len(_handled_oom_kills) > _MAX_HANDLED_OOM_KILLS:
        _handled_oom_kills.pop(next(iter(_handled_oom_kills)))
    return True


def _terminated_state(container_status: dict) -> dict:
    """The last terminated state of this container, or an empty dict."""
    return (container_status.get("lastState", {}) or {}).get("terminated", {}) or {}


def build_oom_kill(pod: dict, container_status: dict) -> ObservedOomKill | None:
    """The OOM kill this container status describes, or None when it is not one.

    Both detection paths build their kill here. That keeps ``finishedAt`` -- the key that
    makes a watch event and a metric hit for the same kill one kill -- provably from the
    same field, and it puts the application-pod guard in one place: the watch gets that
    guard from its label selector, the metric sweep reads a pod by name and has nothing
    but this. Without it a sleep-mode waker (same ``app``/``deployment``/``project``
    labels, hardcoded 64Mi limit) would raise the limit of the component it fronts.
    """
    metadata = pod.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    if not is_application_pod(labels):
        return None

    terminated = _terminated_state(container_status)
    if terminated.get("reason") != "OOMKilled":
        return None

    return ObservedOomKill(
        namespace=metadata.get("namespace", ""),
        pod_name=metadata.get("name", ""),
        container_name=container_status.get("name", ""),
        finished_at=terminated.get("finishedAt", ""),
        project_name=labels.get("project", ""),
        deployment_name=labels.get("deployment", ""),
        app_name=labels.get("app", ""),
        pod_template_hash=labels.get(POD_TEMPLATE_HASH_LABEL) or None,
    )


def observe_pod_restarts(pod: dict, restart_counts: dict[str, int]) -> list[ObservedOomKill]:
    """Return the OOM kills this pod object reports since it was last seen.

    The trigger is a RISEN ``restartCount``, never the presence of a terminated state.
    ``lastState`` keeps describing the same old kill for as long as the pod lives, so
    reading it directly would re-report that one kill on every update.

    A container seen for the first time is only recorded, never reported, so the initial
    list at startup (and the relist after a reconnect) seeds the cache instead of firing
    on every pod that ever restarted.

    Only ``OOMKilled`` is reported: Error and Completed have their own paths, and picking
    them up here would remediate the same thing twice.

    Mutates ``restart_counts`` in place.
    """
    metadata = pod.get("metadata", {}) or {}
    namespace = metadata.get("namespace", "")
    pod_name = metadata.get("name", "")
    kills: list[ObservedOomKill] = []

    for container_status in (pod.get("status", {}) or {}).get("containerStatuses", []) or []:
        container_name = container_status.get("name", "")
        if not container_name:
            continue
        restart_count = int(container_status.get("restartCount") or 0)
        key = _restart_key(namespace, pod_name, container_name)
        previous = restart_counts.get(key)
        restart_counts[key] = restart_count
        if previous is None or restart_count <= previous:
            continue

        kill = build_oom_kill(pod, container_status)
        if kill is None:
            logger.debug(
                "Pod watch: %s/%s container %s restarted (%d -> %d), reason %s, nothing to tune",
                namespace,
                pod_name,
                container_name,
                previous,
                restart_count,
                _terminated_state(container_status).get("reason") or "unknown",
            )
            continue

        kills.append(kill)

    return kills


def forget_pod_restarts(pod: dict, restart_counts: dict[str, int]) -> None:
    """Drop a pod the stream reports as DELETED, so a rollout does not pile up in the cache.

    Only that: a pod that disappears while the stream is down keeps its entry, because the
    cache deliberately survives a reconnect (see ``OomPodWatcher._run``). An int per
    container of a pod that once existed is the cheaper of the two.
    """
    metadata = pod.get("metadata", {}) or {}
    namespace = metadata.get("namespace", "")
    pod_name = metadata.get("name", "")
    prefix = f"{namespace}/{pod_name}/"
    for key in [k for k in restart_counts if k.startswith(prefix)]:
        del restart_counts[key]


def resolve_oom_component(kill: ObservedOomKill, project_data: dict) -> str | None:
    """The component reference this kill belongs to, or None when nothing claims it.

    Matches on the ``app`` label against ``generate_unique_name`` rather than by stripping
    the deployment name off it: that is the same direction the rest of this module resolves
    names in, so a component whose name happens to contain a separator cannot be matched
    to the wrong one.
    """
    deployment = next(
        (d for d in project_data.get("deployments", []) if d.get("name") == kill.deployment_name),
        None,
    )
    if deployment is None:
        return None

    cluster = deployment.get("cluster")
    base_namespace = deployment.get("namespace")
    if not cluster or not base_namespace:
        return None
    if cluster != settings.CLUSTER_MANAGER:
        return None
    if get_prefixed_namespace(cluster, base_namespace) != kill.namespace:
        return None

    for component in deployment.get("components", []) or []:
        reference = component.get("reference", "")
        if not reference or component.get("disabled"):
            continue
        if generate_unique_name(kill.deployment_name, reference) == kill.app_name:
            return reference
    return None


async def handle_observed_oom_kill(kill: ObservedOomKill, source: str) -> bool:
    """Resolve one observed kill to a component and tune it. True when a tune committed."""
    if not _claim_oom_kill(kill):
        logger.debug(
            "Pod watch: OOM on %s/%s container %s at %s was already handled",
            kill.namespace,
            kill.pod_name,
            kill.container_name,
            kill.finished_at,
        )
        return False

    logger.info(
        "Pod watch: OOM kill on %s/%s container %s at %s (project=%s deployment=%s app=%s)",
        kill.namespace,
        kill.pod_name,
        kill.container_name,
        kill.finished_at or "unknown",
        kill.project_name or "unknown",
        kill.deployment_name or "unknown",
        kill.app_name or "unknown",
    )

    if not kill.project_name or not kill.deployment_name or not kill.app_name:
        logger.info("Pod watch: pod %s/%s carries no project labels, nothing to tune", kill.namespace, kill.pod_name)
        return False

    try:
        project_data, _ = get_project_data(kill.project_name)
    except ValueError as e:
        logger.info("Pod watch: no project file for %s, nothing to tune: %s", kill.project_name, e)
        return False

    component_ref = resolve_oom_component(kill, project_data)
    if component_ref is None:
        # Deployments whose manifests do not come from OPI (helmfile) land here: there is
        # nothing in the project file to raise, so reporting is all this can do.
        logger.info(
            "Pod watch: no component of %s/%s claims pod %s in %s, only reporting",
            kill.project_name,
            kill.deployment_name,
            kill.pod_name,
            kill.namespace,
        )
        return False

    if not oom_is_fresh_evidence(kill.project_name, kill.deployment_name, kill.app_name, kill.pod_template_hash):
        logger.info(
            "Pod watch: OOM for %s/%s component %s is on pod generation %s, the same one the previous "
            "tune answered, waiting for that increase to roll out",
            kill.project_name,
            kill.deployment_name,
            component_ref,
            kill.pod_template_hash,
        )
        return False

    return await apply_oom_tune(
        kill.project_name,
        kill.deployment_name,
        [component_ref],
        {kill.app_name: kill.pod_template_hash},
        source=f"{source} ({kill.pod_name} container {kill.container_name})",
    )


# The stream is a sequence of concatenated JSON documents. Measured against kubectl 1.32
# on 29 September, ``--watch --output-watch-events`` writes each one compact on its own
# line, but a chunk boundary still falls wherever the socket happens to break, so reading
# by line is not enough. ``raw_decode`` pulls whole documents off the front of the buffer
# and leaves the unfinished tail alone, which also covers the pretty-printed shape.
#
# The cap is there for the one case raw_decode cannot tell apart from an unfinished
# document: genuinely malformed output (a half-written object when kubectl is killed).
# Without it the buffer would grow until the process is restarted.
_MAX_WATCH_BUFFER_BYTES = 8 * 1024 * 1024


async def read_watch_events(stream: asyncio.StreamReader) -> AsyncIterator[dict]:
    """Yield the ``{"type": ..., "object": ...}`` documents of a kubectl watch stream."""
    decoder = json.JSONDecoder()
    buffer = ""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="replace")
        while True:
            buffer = buffer.lstrip()
            if not buffer:
                break
            try:
                event, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break
            buffer = buffer[end:]
            if isinstance(event, dict):
                yield event
        if len(buffer) > _MAX_WATCH_BUFFER_BYTES:
            logger.warning("Pod watch: dropping %d bytes of unparsable watch output", len(buffer))
            buffer = ""


async def _drain_watch_errors(stream: asyncio.StreamReader | None) -> None:
    """Log whatever kubectl writes to stderr.

    Not optional: an unread stderr pipe fills up and then blocks the process that is
    supposed to be streaming pods.
    """
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        logger.warning("Pod watch: kubectl said: %s", line.decode("utf-8", errors="replace").rstrip())


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
    with contextlib.suppress(ProcessLookupError):
        await process.wait()


class OomPodWatcher:
    """Streams every application pod on this cluster and tunes what OOMs.

    One long-lived ``kubectl get pods -A --watch``: nothing while the cluster is quiet,
    one pod object per change otherwise. Linear in changes, not in pods.
    """

    def __init__(self, cluster: str) -> None:
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None
        self._restart_counts: dict[str, int] = {}

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="oom-pod-watch")
        logger.info("OOM pod watch started (cluster=%s)", self._cluster)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("OOM pod watch stopped")

    async def _run(self) -> None:
        delay = POD_WATCH_RECONNECT_SECONDS
        while self._running:
            handled = await self._watch_once()
            if not self._running:
                break
            # The cache deliberately SURVIVES the reconnect. The relist replays every pod,
            # so a container whose restartCount grew while the stream was down shows up as
            # a rise against the remembered value and is caught after all.
            if handled >= _POD_WATCH_HEALTHY_EVENTS:
                delay = POD_WATCH_RECONNECT_SECONDS
            logger.info("OOM pod watch: stream ended after %d event(s), relisting in %ds", handled, delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            delay = min(delay * 2, POD_WATCH_MAX_RECONNECT_SECONDS)

    async def _watch_once(self) -> int:
        """Run one watch stream to its end. Returns how many events it delivered."""
        kubectl = KubectlConnector()
        process = await kubectl.watch_pods(all_application_pods_selector())
        if process is None or process.stdout is None:
            return 0

        stderr_task = asyncio.create_task(_drain_watch_errors(process.stderr))
        seen = 0
        try:
            async for event in read_watch_events(process.stdout):
                if not await self._handle_event(event):
                    break
                seen += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OOM pod watch: error while reading the stream")
        finally:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            await _stop_process(process)
        return seen

    async def _handle_event(self, event: dict) -> bool:
        """Handle one watch event. False means the stream is over and must be relisted.

        The ERROR event is the one that is not a pod: on an expired watch the API server
        sends a ``Status`` (``reason: Expired``, ``code: 410``) and kubectl passes it on.
        It has to end the round, and it must NOT count as an event, or a watch that keeps
        being refused reads as a stream that ran and comes back every five seconds.
        """
        event_type = event.get("type", "")
        pod = event.get("object", {})
        if event_type == "ERROR":
            status = pod if isinstance(pod, dict) else {}
            logger.warning(
                "OOM pod watch: the API server ended the stream (%s, code %s): %s",
                status.get("reason", ""),
                status.get("code", ""),
                status.get("message", ""),
            )
            return False
        if not isinstance(pod, dict):
            return True
        if event_type == "DELETED":
            forget_pod_restarts(pod, self._restart_counts)
            return True
        for kill in observe_pod_restarts(pod, self._restart_counts):
            await handle_observed_oom_kill(kill, source="pod watch")
        return True


class OomMetricSweeper:
    """Hourly safety net: the OOM kills the watch did not see.

    The seconds around an OPI deploy, and a kill on a pod that was not in the cache yet.
    """

    def __init__(self, cluster: str, interval_seconds: int) -> None:
        self._cluster = cluster
        self._interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="oom-metric-sweep")
        logger.info("OOM metric sweep started (cluster=%s, every %ds)", self._cluster, self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("OOM metric sweep stopped")

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            try:
                await run_oom_metric_sweep(self._cluster)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("OOM metric sweep failed")


async def read_pod_oom_kill(namespace: str, pod_name: str, container_name: str) -> ObservedOomKill | None:
    """Read one pod and return its OOM kill for this container, or None.

    This is what keeps the metric from deciding anything: a hit only becomes a kill once
    ``build_oom_kill`` accepts the pod status, which is also where this path gets the
    application-pod guard the watch gets from its selector.
    """
    kubectl = KubectlConnector()
    if not KubectlConnector.isConnected:
        return None
    try:
        stdout, stderr, code = await kubectl.run_command(["get", "pod", "-n", namespace, pod_name, "-o", "json"])
        if code != 0:
            logger.debug("OOM metric sweep: could not read pod %s/%s: %s", namespace, pod_name, stderr)
            return None
        pod = json.loads(stdout)
    except (KubectlConnectionError, KubectlExecutionError, json.JSONDecodeError) as e:
        logger.debug("OOM metric sweep: could not read pod %s/%s: %s", namespace, pod_name, e)
        return None

    for container_status in (pod.get("status", {}) or {}).get("containerStatuses", []) or []:
        if container_status.get("name") != container_name:
            continue
        return build_oom_kill(pod, container_status)
    return None


async def run_oom_metric_sweep(cluster: str) -> int:
    """One pass of the safety net. Returns how many kills it tuned."""
    query = _OOM_METRIC_QUERY.format(prefix=get_namespace_prefix(cluster))
    try:
        connector = await get_metrics_connector()
        rows = await connector.custom_query(query)
    except Exception as e:
        logger.info("OOM metric sweep: disabled, no metrics backend answered the query: %s", e)
        return 0

    tuned = 0
    for row in rows or []:
        metric = row.get("metric", {}) or {}
        namespace = metric.get("namespace", "")
        pod_name = metric.get("pod", "")
        container_name = metric.get("container", "")
        if not namespace or not pod_name or not container_name:
            continue
        kill = await read_pod_oom_kill(namespace, pod_name, container_name)
        if kill is None:
            continue
        if await handle_observed_oom_kill(kill, source="metric sweep"):
            tuned += 1

    logger.info("OOM metric sweep: %d metric hit(s), %d tune(s) started", len(rows or []), tuned)
    return tuned
