"""Live-cluster verification helpers for sandbox E2E tests.

Thin ``kubectl`` wrappers used to prove that a project's services were actually
provisioned into the cluster (namespace secrets, PVCs, ingress, sidecars, ...),
not just declared in the project YAML.

kubectl talks to whatever context is active (the sandbox is a local Kind cluster,
context ``kind-rig-sandbox``). When kubectl is not installed or cannot reach a
cluster, ``kubectl_available()`` returns False so the caller can skip cleanly -
these checks only make sense on the machine that runs the sandbox.
"""

from __future__ import annotations

import json
import subprocess
import time
from functools import lru_cache
from typing import Any


def _run(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@lru_cache(maxsize=1)
def kubectl_available() -> bool:
    """True if kubectl is installed and can reach the current cluster."""
    try:
        return _run(["get", "namespaces"], timeout=15.0).returncode == 0
    except FileNotFoundError:
        return False
    except subprocess.SubprocessError:
        return False


def namespace_exists(namespace: str) -> bool:
    return _run(["get", "namespace", namespace]).returncode == 0


def get_json(kind: str, namespace: str) -> dict[str, Any]:
    """Return the parsed ``kubectl get <kind> -n <namespace> -o json`` output."""
    result = _run(["get", kind, "-n", namespace, "-o", "json"])
    if result.returncode != 0:
        return {"items": []}
    return json.loads(result.stdout or '{"items": []}')


def resource_names(kind: str, namespace: str) -> list[str]:
    return [item["metadata"]["name"] for item in get_json(kind, namespace).get("items", [])]


def resource_names_by_label(kind: str, namespace: str, label_selector: str) -> list[str]:
    """Names of ``kind`` in ``namespace`` matching a ``key=value`` label selector."""
    result = _run(["get", kind, "-n", namespace, "-l", label_selector, "-o", "name"])
    if result.returncode != 0:
        return []
    return [line.split("/", 1)[-1] for line in result.stdout.split() if line.strip()]


def _project_namespaces(project_name: str) -> list[str]:
    """OPI-created namespaces belonging to a project (main + any infra namespace).

    Matches on the ``created-by=operations-manager`` label and the project name in
    the namespace name, so it catches ``rig-<project>`` and any infra-namespace
    variant without hard-coding the cluster prefix.
    """
    result = _run(["get", "namespaces", "-l", "created-by=operations-manager", "-o", "name"])
    if result.returncode != 0:
        return []
    names = [line.split("/", 1)[-1] for line in result.stdout.split() if line.strip()]
    return [ns for ns in names if project_name in ns]


def force_cleanup_project(project_name: str) -> None:
    """Best-effort teardown net: remove any namespace / ArgoCD app the project
    delete left behind. Never raises - teardown must not fail a test.

    The project delete only drops the namespace once the ArgoCD app deletion is
    *confirmed*; on a busy sandbox that confirmation often times out, orphaning the
    (now empty) namespace and a dangling ``Unknown`` ArgoCD app. Left unchecked
    these accumulate across runs and starve the cluster, which in turn makes the
    provisioning-wait tests time out. This reclaims them directly.
    """
    if not kubectl_available():
        return
    argo_ns = "rig-system"
    # ArgoCD apps for this project. Mark for deletion, then clear the
    # resources-finalizer (to an empty list - a null merge-patch is ignored) so the
    # delete completes instead of hanging on ArgoCD pruning a namespace we remove
    # below.
    for app in resource_names_by_label("applications", argo_ns, f"project={project_name}"):
        _run(["delete", "application", app, "-n", argo_ns, "--ignore-not-found", "--wait=false"])
        _run(["patch", "application", app, "-n", argo_ns, "--type", "merge", "-p", '{"metadata":{"finalizers":[]}}'])
    # The per-project ArgoCD AppProject.
    _run(["delete", "appproject", project_name, "-n", argo_ns, "--ignore-not-found", "--wait=false"])
    # The project namespaces (main + any infra). --wait=false so a slow drain does
    # not stall teardown; the namespace finalizer completes asynchronously.
    for namespace in _project_namespaces(project_name):
        _run(["delete", "namespace", namespace, "--ignore-not-found", "--wait=false"])


def wait_for(predicate, *, timeout: float, interval: float = 5.0) -> bool:
    """Poll ``predicate()`` until true or timeout. Returns the final truthiness."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def pod_container_names(namespace: str) -> list[str]:
    """All container names across all pods in the namespace (incl. sidecars)."""
    names: list[str] = []
    for pod in get_json("pods", namespace).get("items", []):
        names.extend(container["name"] for container in pod.get("spec", {}).get("containers", []))
    return names


def cnpg_cluster_names(namespace: str) -> list[str]:
    """CNPG ``postgresql.cnpg.io/Cluster`` names in the namespace (dedicated
    namespace-postgres databases)."""
    return resource_names("clusters.postgresql.cnpg.io", namespace)


def deployment_pod_annotations(namespace: str) -> dict[str, str]:
    """Merged pod-template annotations across the namespace's deployments."""
    annotations: dict[str, str] = {}
    for deploy in get_json("deployments", namespace).get("items", []):
        annotations.update(deploy.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}) or {})
    return annotations
