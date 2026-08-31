"""Pure-data diagnostics for deployments.

This module is the single source of truth for "what is broken with this
deployment" — used by both the web UI (which adds presentation) and the
V2 read API (which returns raw entries).

Helpers do not raise on partial failure: every sub-fetch is best-effort
and logs at debug level. The returned shape is always valid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opi.api.v2.models import ErrorCategory
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.extensions.pipeline import get_registry_rewrite_mappings
from opi.extensions.registry_rewrite import original_image
from opi.manager.project_validation import _split_image_reference
from opi.services.event_interpreter import _friendly_resource_name
from opi.utils.naming import generate_unique_name

if TYPE_CHECKING:
    from opi.connectors.argo import ArgoConnector
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)

# Event kinds whose Argo tree health is a reliable "is it resolved" signal:
# a Healthy pod runs, a Healthy PVC is Bound. Events on these objects are
# dropped once the object is healthy again (or gone).
_TREE_VERIFIED_EVENT_KINDS = frozenset({"Pod", "PersistentVolumeClaim"})


_CATEGORY_EXPLANATIONS: dict[ErrorCategory, str] = {
    ErrorCategory.ImagePull: (
        "The container image could not be pulled. Check the image name, tag, and registry credentials."
    ),
    ErrorCategory.CrashLoop: (
        "The container starts but exits or crashes repeatedly. Inspect the container's own logs for the cause."
    ),
    ErrorCategory.OutOfMemory: (
        "The container ran out of memory and was killed. Increase the memory limit or reduce memory usage."
    ),
    ErrorCategory.HealthCheck: (
        "The container is running but failing health checks. Check liveness/readiness probe configuration."
    ),
    ErrorCategory.SyncFailed: (
        "The cluster could not apply the desired state. "
        "Check the synced manifest for errors and the operation message for details."
    ),
    ErrorCategory.ComparisonError: (
        "The cluster could not compare the desired and live state. Often a manifest validation error."
    ),
}


def categorize_error(resource: str, message: str) -> tuple[ErrorCategory, str | None]:
    """Map a (resource, message) pair to an ErrorCategory and human explanation.

    Order matters: more specific keywords are checked before broader ones.
    Returns (Unknown, None) when no pattern matches.
    """
    msg_lower = message.lower()

    if resource == "SyncOperation" or (resource.startswith("Event/") and "syncfailed" in msg_lower):
        category = ErrorCategory.SyncFailed
    elif resource == "ComparisonError":
        category = ErrorCategory.ComparisonError
    elif "imagepullbackoff" in msg_lower or "errimagepull" in msg_lower or "back-off pulling image" in msg_lower:
        category = ErrorCategory.ImagePull
    elif "oomkilled" in msg_lower or "out of memory" in msg_lower or "outofmemory" in msg_lower:
        category = ErrorCategory.OutOfMemory
    elif "crashloopbackoff" in msg_lower or "back-off restarting" in msg_lower:
        category = ErrorCategory.CrashLoop
    elif "liveness probe" in msg_lower or "readiness probe" in msg_lower or "startup probe" in msg_lower:
        category = ErrorCategory.HealthCheck
    elif "syncfailed" in msg_lower or "sync operation failed" in msg_lower:
        category = ErrorCategory.SyncFailed
    else:
        category = ErrorCategory.Unknown

    return category, _CATEGORY_EXPLANATIONS.get(category)


def conditions_to_errors(status_data: dict[str, Any]) -> list[dict[str, str]]:
    """App-level ArgoCD conditions as error entries - the cheap part of the diagnostics.

    Reads only ``status.conditions[]`` from ``status_data`` (no extra API calls), so it can
    run unconditionally - even for a deployment whose health still reads ``Healthy`` from the
    last good reconciliation while a fresh ``ComparisonError`` blocks the current compare.
    ``gather_deployment_errors`` already includes these conditions in its (more expensive)
    output, so callers use one or the other, not both.
    """
    errors: list[dict[str, str]] = []
    status = status_data.get("status", {}) or {}
    for condition in status.get("conditions", []) or []:
        condition_msg = condition.get("message", "")
        if not condition_msg:
            continue
        errors.append({"resource": condition.get("type", "Unknown"), "message": condition_msg})
    return errors


async def gather_deployment_errors(
    *,
    argo: ArgoConnector,
    kubectl: KubectlConnector | None,
    app_name: str,
    base_namespace: str,
    cluster: str,
    deployment_name: str,
    status_data: dict[str, Any],
    disabled_components: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Collect raw error entries for a deployment.

    Sources:
      1. ``status.resources[]``: non-Healthy entries with messages
      2. ``application_resource_tree``: Pod / ReplicaSet messages (extra Argo call)
      3. ``operationState.syncResult.resources[]``: SyncFailed entries
      4. ``status.conditions[]``: app-level conditions
      5. ``operationState.phase`` Failed / Error: the operation message
      6. namespace events (kubectl): only when the application is already
         Degraded or other sources have produced errors

    Events are history, not state: pod and PVC events are verified against
    the resource tree (the current ground truth) and dropped when their
    object is now Healthy (pod running, PVC Bound) or no longer exists, so
    resolved hiccups (e.g. a FailedScheduling or ProvisioningFailed while a
    volume was still provisioning) are not shown as live errors. When the
    tree fetch failed, events pass unverified.

    ``disabled_components`` (WP6): the friendly names (= references) of components
    intentionally scaled to zero -- the OOM/image-pull watcher's auto-disable and
    manual disables both write ``disabled`` per deployment-component. Their resources
    are at their intended end state (0 replicas), so any lingering "waiting for
    rollout"/"pods being created" (Progressing) or old-pod message they carry is state,
    not a live problem; the deployment card already surfaces them as *disabled*. These
    entries are dropped so a disabled component is not reported as busy.

    Each entry: ``{"resource": str, "message": str, "timestamp": str?}``.
    All sub-fetches are best-effort: failures log at debug, never raise.
    """
    errors: list[dict[str, str]] = []
    status = status_data.get("status", {}) or {}
    health = status.get("health", {}) or {}
    operation_state = status.get("operationState", {}) or {}
    app_health = health.get("status", "Unknown")

    for resource in status.get("resources", []) or []:
        resource_health = resource.get("health", {}) or {}
        health_status = resource_health.get("status")
        health_msg = resource_health.get("message", "")
        if not health_msg and health_status not in ("Degraded", "Missing"):
            continue
        resource_label = f"{resource.get('kind', 'Resource')}/{resource.get('name', 'unknown')}"
        if health_status in ("Degraded", "Missing"):
            errors.append({"resource": resource_label, "message": health_msg or "Unknown error"})
        elif health_status == "Progressing" and health_msg:
            errors.append({"resource": resource_label, "message": health_msg})

    tree_available = True
    try:
        tree_nodes = await argo.get_application_resource_tree(app_name)
    except Exception as exc:
        logger.debug("Could not fetch resource tree for %s: %s", app_name, exc)
        tree_nodes = []
        tree_available = False

    for node in tree_nodes:
        node_kind = node.get("kind", "")
        if node_kind not in ("Pod", "ReplicaSet"):
            continue
        node_health = node.get("health", {}) or {}
        node_health_status = node_health.get("status")
        node_health_msg = node_health.get("message", "")
        if not node_health_msg or node_health_status not in ("Degraded", "Missing"):
            continue
        entry: dict[str, str] = {
            "resource": f"{node_kind}/{node.get('name', 'unknown')}",
            "message": node_health_msg,
        }
        node_created = node.get("createdAt")
        if node_created:
            entry["timestamp"] = node_created
        errors.append(entry)

    if kubectl is not None and (app_health == "Degraded" or errors):
        try:
            k8s_ns = get_prefixed_namespace(cluster, base_namespace)
            raw_events = await kubectl.get_namespace_events(k8s_ns, limit=30)
        except Exception as exc:
            logger.debug("Could not fetch namespace events for %s: %s", deployment_name, exc)
            raw_events = []

        tree_health: dict[tuple[str, str], str] = {
            (node.get("kind", ""), node.get("name", "")): (node.get("health", {}) or {}).get("status") or ""
            for node in tree_nodes
            if node.get("kind") in _TREE_VERIFIED_EVENT_KINDS
        }

        for event in raw_events:
            obj = event.get("object", "unknown")
            if obj != deployment_name and not obj.startswith(f"{deployment_name}-"):
                continue
            kind = event.get("kind", "")
            if tree_available and kind in _TREE_VERIFIED_EVENT_KINDS:
                current_health = tree_health.get((kind, obj))
                if current_health is None or current_health == "Healthy":
                    continue
            msg = event.get("message", "")
            if not msg:
                continue
            entry = {
                "resource": f"Event/{obj}",
                "message": f"[{event.get('reason', '')}] {msg}",
            }
            event_time = event.get("time")
            if event_time:
                entry["timestamp"] = event_time
            errors.append(entry)

    sync_result = operation_state.get("syncResult", {}) or {}
    for resource in sync_result.get("resources", []) or []:
        if resource.get("status") != "SyncFailed":
            continue
        errors.append(
            {
                "resource": f"{resource.get('kind', 'Resource')}/{resource.get('name', 'unknown')}",
                "message": resource.get("message", "Sync failed"),
            }
        )

    for condition in status.get("conditions", []) or []:
        condition_msg = condition.get("message", "")
        if not condition_msg:
            continue
        errors.append({"resource": condition.get("type", "Unknown"), "message": condition_msg})

    if operation_state.get("phase") in ("Failed", "Error"):
        op_entry: dict[str, str] = {
            "resource": "SyncOperation",
            "message": operation_state.get("message", "Sync operation failed"),
        }
        finished_at = operation_state.get("finishedAt")
        if finished_at:
            op_entry["timestamp"] = finished_at
        errors.append(op_entry)

    if disabled_components:
        # Drop entries for a disabled component's own resources (Deployment/ReplicaSet/
        # Pod/Event named ``{deployment}-{reference}-...``). App-level entries
        # (SyncOperation, conditions) do not carry the deployment prefix, so their
        # friendly name never matches a reference and they are kept.
        errors = [
            entry
            for entry in errors
            if _friendly_resource_name(entry["resource"], deployment_name) not in disabled_components
        ]

    return errors


def gather_sync_deviations(
    status_data: dict[str, Any],
    *,
    deployment_name: str = "",
    disabled_components: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """List the resources that keep a deployment away from Synced/Healthy, with a reason.

    The counterpart of :func:`gather_deployment_errors` for *deviations*: entries that
    explain a yellow badge without being an application problem. Two sources, both read
    from the already-fetched Application payload (no extra API calls):

    1. App sync ``OutOfSync``: every OutOfSync resource. A ``requiresPruning`` resource
       that the last (Succeeded) sync already reported as ``Pruned`` still being here
       means the cluster cannot finish the delete (e.g. a stuck finalizer) -- that gets
       its own reason, because no amount of re-syncing will resolve it.
    2. App health ``Progressing``: every Progressing resource *without* a health message.
       Entries with a message already surface through ``gather_deployment_errors``;
       without one the yellow health badge was previously unexplained.

    Entries: ``{"resource": "Kind/name", "kind": str, "reason": str}``. Resources of
    disabled components are dropped, like in ``gather_deployment_errors``.
    """
    status = status_data.get("status", {}) or {}
    sync_status = (status.get("sync") or {}).get("status")
    health_status = (status.get("health") or {}).get("status")
    operation_state = status.get("operationState", {}) or {}
    resources = status.get("resources", []) or []
    # Sleutel-aanwezigheid, niet truthiness: ArgoCD's ``automated: {}`` betekent aan.
    auto_sync = "automated" in (((status_data.get("spec") or {}).get("syncPolicy")) or {})

    # Resources the last successful sync already deleted; if such a resource still
    # exists, its deletion is stuck and the next sync will not change that.
    pruned_by_last_sync: set[tuple[str, str]] = set()
    if operation_state.get("phase") == "Succeeded":
        for result in (operation_state.get("syncResult", {}) or {}).get("resources", []) or []:
            if result.get("status") == "Pruned":
                pruned_by_last_sync.add((result.get("kind", ""), result.get("name", "")))

    deviations: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(resource: dict[str, Any], reason: str) -> None:
        kind = resource.get("kind", "Resource")
        label = f"{kind}/{resource.get('name', 'unknown')}"
        if label in seen:
            return
        seen.add(label)
        deviations.append({"resource": label, "kind": kind, "reason": reason})

    if sync_status == "OutOfSync":
        for resource in resources:
            if resource.get("status") != "OutOfSync":
                continue
            if resource.get("requiresPruning"):
                if (resource.get("kind", ""), resource.get("name", "")) in pruned_by_last_sync:
                    reason = "is verwijderd, maar het cluster maakt de verwijdering niet af"
                else:
                    reason = "staat niet meer in git en wordt bij de volgende sync opgeruimd"
            elif auto_sync:
                reason = "wijkt af van git en wordt bij de volgende sync bijgewerkt"
            else:
                reason = "wijkt af van git; auto-sync staat uit"
            _add(resource, reason)

    if health_status == "Progressing":
        for resource in resources:
            resource_health = resource.get("health", {}) or {}
            if resource_health.get("status") != "Progressing" or resource_health.get("message"):
                continue
            _add(resource, "nog bezig")

    if disabled_components:
        deviations = [
            entry
            for entry in deviations
            if _friendly_resource_name(entry["resource"], deployment_name) not in disabled_components
        ]

    return deviations


@dataclass(frozen=True)
class ComponentPodSummary:
    """What is actually serving traffic for one component of a deployment.

    ``is_serving`` False is a RESULT, not a missing value: it says the platform looked and
    found no pod behind the Service. That is the difference between "the rollout of a new
    version failed while the previous one keeps serving" and "the application is down",
    and those two deserve opposite words on the card.
    """

    #: The component reference as it stands in the project file.
    reference: str
    #: Whether a pod is serving traffic for this component right now.
    is_serving: bool
    #: The serving pod's name, or None when nothing serves.
    pod_name: str | None = None
    #: The image it actually runs, in its SOURCE-registry spelling (not the proxy rewrite).
    image: str | None = None
    #: ``state.running.startedAt`` of its ``app`` container.
    running_since: str | None = None
    #: The image the project file configures for this component, source-registry spelling.
    configured_image: str | None = None
    #: Whether the running image is the configured one. ``None`` means the question was
    #: not answerable: a digest reference and a tag reference say nothing about each other,
    #: so a mismatch between them is not a finding.
    runs_configured_image: bool | None = None


def summarize_component_pods(
    pods: list[dict[str, Any]],
    *,
    deployment: dict[str, Any],
) -> list[ComponentPodSummary]:
    """Per component of ``deployment``: what is serving, on which image, since when.

    ``pods`` is what :meth:`KubectlConnector.get_application_pods` returned for this
    deployment.

    Pods are matched to components through a pre-built ``{unique name: reference}`` map
    rather than by stripping the deployment name off the ``app`` label:
    ``generate_unique_name`` is the function that produced those names and is free to stop
    being a plain join, and a pod attributed to the wrong component is worse than no pod
    at all.

    Components the project file marks ``disabled`` are left out entirely. Their zero
    replicas are the intended end state, the card already names them and their reason, and
    a red "nothing is running" next to that would contradict it.
    """
    mappings = get_registry_rewrite_mappings(settings.CLUSTER_MANAGER)
    deployment_name = deployment.get("name") or ""

    components = [
        comp
        for comp in deployment.get("components", []) or []
        if isinstance(comp, dict) and comp.get("reference") and not comp.get("disabled")
    ]
    reference_by_unique_name = {
        generate_unique_name(deployment_name, comp["reference"]): comp["reference"] for comp in components
    }

    # The serving pod: not being deleted, and its ``app`` container reports ready. A pod
    # that is terminating still carries the label and would otherwise look like the answer
    # during the seconds a rollout takes to hand over.
    serving_by_reference: dict[str, dict[str, Any]] = {}
    for pod in pods:
        reference = reference_by_unique_name.get(pod.get("app", ""))
        if reference is None or reference in serving_by_reference:
            continue
        if pod.get("deleting") or not pod.get("ready"):
            continue
        serving_by_reference[reference] = pod

    summaries: list[ComponentPodSummary] = []
    for comp in components:
        reference = comp["reference"]
        configured = comp.get("image")
        configured_source = original_image(configured, mappings) if isinstance(configured, str) and configured else None

        pod = serving_by_reference.get(reference)
        if pod is None:
            summaries.append(
                ComponentPodSummary(reference=reference, is_serving=False, configured_image=configured_source)
            )
            continue

        running_source = original_image(pod.get("image", ""), mappings) or None
        summaries.append(
            ComponentPodSummary(
                reference=reference,
                is_serving=True,
                pod_name=pod.get("name") or None,
                image=running_source,
                running_since=pod.get("started_at"),
                configured_image=configured_source,
                runs_configured_image=_compare_image_references(running_source, configured_source),
            )
        )

    return summaries


def _compare_image_references(running: str | None, configured: str | None) -> bool | None:
    """Whether ``running`` and ``configured`` are the same image, or None when unanswerable.

    A digest reference and a tag reference name the same image just as often as they name
    different ones -- ``app:2.1`` and ``app@sha256:...`` are simply not comparable as
    strings. Saying "it runs a different image" on that basis would be a guess dressed as
    a fact, so only same-shape references get a verdict.
    """
    if not running or not configured:
        return None
    _, _, running_has_digest = _split_image_reference(running)
    _, _, configured_has_digest = _split_image_reference(configured)
    if running_has_digest != configured_has_digest:
        return None
    return running == configured
