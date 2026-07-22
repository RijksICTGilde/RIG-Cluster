"""Pure-data diagnostics for deployments.

This module is the single source of truth for "what is broken with this
deployment" — used by both the web UI (which adds presentation) and the
V2 read API (which returns raw entries).

Helpers do not raise on partial failure: every sub-fetch is best-effort
and logs at debug level. The returned shape is always valid.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.api.v2.models import ErrorCategory
from opi.core.cluster_config import get_prefixed_namespace

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


async def gather_deployment_errors(
    *,
    argo: ArgoConnector,
    kubectl: KubectlConnector | None,
    app_name: str,
    base_namespace: str,
    cluster: str,
    deployment_name: str,
    status_data: dict[str, Any],
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

    return errors
