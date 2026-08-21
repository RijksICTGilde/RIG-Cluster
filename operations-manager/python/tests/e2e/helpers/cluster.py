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

import contextlib
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator


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


def wait_for_project_apps_healthy(project_name: str, *, timeout: float, interval: float = 5.0) -> bool:
    """Wait until the project's ArgoCD app(s) exist and report health ``Healthy``.

    This is the same condition ``create_project`` blocks on, so a create fixture
    that waits for it no longer returns (and tears down) while the async
    create_project task is still provisioning - which would otherwise delete the
    app out from under the running task and jam the worker.

    Best-effort: returns True immediately when kubectl is unavailable (so tests in
    a kubectl-less environment keep their previous behaviour) and False on timeout
    (the caller proceeds either way - this only paces teardown, it is not an
    assertion).
    """
    if not kubectl_available():
        return True

    def _healthy() -> bool:
        result = _run(["get", "applications", "-n", "rig-system", "-l", f"project={project_name}", "-o", "json"])
        if result.returncode != 0:
            return False
        items = json.loads(result.stdout or '{"items": []}').get("items", [])
        if not items:
            return False
        return all((app.get("status", {}).get("health", {}).get("status") == "Healthy") for app in items)

    return wait_for(_healthy, timeout=timeout, interval=interval)


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


def running_pod_names(namespace: str, name_prefix: str) -> list[str]:
    """Running pod names in the namespace whose name starts with ``name_prefix``.

    Used to find a component's application pod (named after its deployment),
    excluding db/infra pods that live in other namespaces.
    """
    names: list[str] = []
    for pod in get_json("pods", namespace).get("items", []):
        if pod.get("status", {}).get("phase") != "Running":
            continue
        name = pod.get("metadata", {}).get("name", "")
        if name.startswith(name_prefix):
            names.append(name)
    return names


def _free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def port_forward(namespace: str, pod: str, remote_port: int, *, ready_timeout: float = 30.0) -> Generator[str]:
    """A ``kubectl port-forward`` that stays up for the length of the block.

    The subprocess lifecycle lives here and nowhere else: ``http_get_via_port_forward``
    below is this block plus a GET. A BROWSER needs the forward to outlive that first
    request - it loads the page, posts a form and reads the answer, all against the same
    address. Yields the base URL to point it at.

    Waits until the local port actually accepts a connection before yielding, so a failure
    inside the block is about the page and not about a forward that was not up yet.
    """
    local_port = _free_local_port()
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, f"pod/{pod}", f"{local_port}:{remote_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(f"port-forward exited early: {stderr.strip()}")
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", local_port), timeout=2.0):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"port-forward to {pod}:{remote_port} was not up within {ready_timeout:.0f}s")
        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.SubprocessError):
            proc.wait(timeout=5)


def http_get_via_port_forward(
    namespace: str,
    pod: str,
    remote_port: int,
    path: str,
    *,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """GET ``path`` from a pod's ``remote_port`` over a temporary ``kubectl
    port-forward``.

    This reaches the application container directly, bypassing any ingress and
    the authorization-wall sidecar (which would otherwise gate the request behind
    OIDC). The workload image is distroless, so ``kubectl exec`` + curl is not an
    option - port-forward is the deterministic path. Returns (status_code, body).

    ``timeout`` bounds each of the two waits: first the forward coming up, then the GET
    succeeding. The retry on the GET stays, because a forward that accepts a connection
    does not mean the app inside the pod is already serving.
    """
    with port_forward(namespace, pod, remote_port, ready_timeout=timeout) as base_url:
        url = f"{base_url}{path}"
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5.0) as resp:  # noqa: S310 (fixed localhost)
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:
                # A response with a non-2xx status (e.g. ?strict=1 -> 503) still
                # carries the JSON body we want to assert on.
                return exc.code, exc.read().decode()
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_error = exc
                time.sleep(1.0)
        raise RuntimeError(f"GET {url} did not succeed within {timeout:.0f}s: {last_error}")


#: The annotation the ArgoCD CMP plugin stamps on every pod template: a hash over all
#: Secrets and ConfigMaps in the Application. It is what makes a pod restart when only the
#: CONTENT of a secret changes, and it is injected at render time -- so it appears on the
#: cluster and in no template under ``manifests/``. Pinned here as a literal because the
#: producer is a shell/yq filter in bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml
#: that cannot be imported.
CONFIG_HASH_ANNOTATION = "checksum/config"


def probe_in_pod(namespace: str, pod: str, script: str, *, probe: str, target: str = "app") -> str | None:
    """Run a shell snippet against the target container's process and return its output.

    ``kubectl exec`` is not an option here: the application images are distroless, so they
    carry no shell and no coreutils, and every exec fails with "executable file not found".
    An ephemeral debug container that shares the target's process namespace does have a
    shell, and reaches the target through ``/proc/1`` -- its environment as the process
    actually received it, and its filesystem as the container actually sees it.

    That distinction is the whole point of these measurements: ``envFrom`` is injected once
    at container start and a ``subPath`` mount is a one-time copy, so a value read this way
    is proof of what the RUNNING container got, not of what the cluster currently holds.

    Returns None when the probe could not be run or produced nothing.
    """
    started = _run(
        [
            "debug",
            pod,
            "-n",
            namespace,
            "--image=busybox:1.36",
            f"--target={target}",
            "-q",
            "--attach=false",
            "-c",
            probe,
            "--",
            "sh",
            "-c",
            script,
        ],
        timeout=120.0,
    )
    if started.returncode != 0:
        return None
    if not wait_for(
        lambda: (
            _run(
                [
                    "get",
                    "pod",
                    pod,
                    "-n",
                    namespace,
                    "-o",
                    f'jsonpath={{.status.ephemeralContainerStatuses[?(@.name=="{probe}")].state.terminated.reason}}',
                ],
            ).stdout.strip()
            == "Completed"
        ),
        timeout=120,
        interval=3,
    ):
        return None
    logs = _run(["logs", pod, "-n", namespace, "-c", probe], timeout=60.0)
    return logs.stdout.strip() if logs.returncode == 0 else None


def read_file_in_pod(namespace: str, pod: str, path: str, *, probe: str) -> str | None:
    """Read one file out of the target container, through its own process view."""
    return probe_in_pod(namespace, pod, f"cat /proc/1/root{path} 2>/dev/null", probe=probe)


def env_in_pod(namespace: str, pod: str, name: str, *, probe: str) -> str | None:
    """Read one environment variable as the target process actually received it."""
    output = probe_in_pod(namespace, pod, f'tr "\\0" "\\n" < /proc/1/environ | grep "^{name}="', probe=probe)
    return output.split("=", 1)[1] if output and "=" in output else None
