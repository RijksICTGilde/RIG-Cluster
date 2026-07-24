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


def deployment_pod_annotations(namespace: str) -> dict[str, str]:
    """Merged pod-template annotations across the namespace's deployments."""
    annotations: dict[str, str] = {}
    for deploy in get_json("deployments", namespace).get("items", []):
        annotations.update(deploy.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}) or {})
    return annotations
