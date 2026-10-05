"""Which application pods cannot pull their image, as a live picture of the cluster.

This exists because the intervention it replaces is gone. Until RC-243 a component that
could not pull was scaled to zero replicas, which made the problem tidy: the pod was no
longer there, and ArgoCD called the application Healthy. That tidiness is what three
outages were paid for, so it was taken away. What is left is a pod in ImagePullBackOff
that stays there until someone fixes the image, and nothing about that is visible by
itself.

So it is made visible twice, from one observation:

- a gauge per cluster, read by the collector in ``opi/core/metrics.py`` and alerted on by
  ``prometheusrule-image-pull.yaml``, so nobody has to notice;
- the deployment card, which names the component, its image, what the registry answered
  and since when, next to (and distinct from) the components OPI really did switch off.

A periodic LIST, not the pod watch of ``oom_watcher``. Two reasons, and the second is the
one that decides it: the watch is off on every cluster until it has proven itself
(``watches_pods_for_oom``) and this has to work on production today; and a snapshot that
is recounted from scratch cannot carry a stale entry, while a stream that misses a DELETED
would keep alerting on a pod that is long gone.

The snapshot is process-local and lives exactly as long as the process. Nothing reads it
to decide anything: it is a report.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass

from opi.connectors.kubectl import KubectlConnector
from opi.handlers.project_file_handler import IMAGE_PULL_REASONS, classify_image_pull_failure
from opi.services.catalog.base import (
    APPLICATION_CONTAINER_NAME,
    all_application_pods_selector,
    is_application_pod,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImagePullFailure:
    """One application pod that cannot pull an image, as the cluster reports it."""

    namespace: str
    pod_name: str
    #: The ``app`` label: the component's unique (deployment-scoped) name.
    app: str
    project: str
    deployment: str
    #: Which container failed. The main one takes precedence over a sidecar, so a
    #: platform sidecar's pull failure never masquerades as the component's own image.
    container: str
    image: str
    #: ``"<waiting reason>: <kubelet message>"``, the registry's own words, unabridged.
    message: str
    #: ``absent`` | ``capacity`` | ``undiagnosed`` (see ``classify_image_pull_failure``).
    reason_class: str
    #: When the pod was created. The container never started, so this is how long the
    #: pull has been failing. Not the waiting state, which carries no timestamp.
    since: str


def read_image_pull_failure(pod: dict) -> ImagePullFailure | None:
    """The image-pull failure this pod reports, or None when it reports none.

    Terminating pods are skipped: during a rollout the replaced pod keeps its labels and
    its state for as long as it takes to hand over.

    A pod of a SUPERSEDED generation is deliberately NOT skipped, unlike in
    ``check_pod_health``. There the question is "did this rollout fail", and a dead
    generation cannot answer it. Here the question is "is a pull failing right now", and
    the normal shape of a bad image push is exactly that: the previous generation keeps
    serving while the new pod cannot pull. That pod is the finding.
    """
    metadata = pod.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    if not is_application_pod(labels):
        return None
    if metadata.get("deletionTimestamp"):
        return None

    found: ImagePullFailure | None = None
    for container_status in (pod.get("status", {}) or {}).get("containerStatuses", []) or []:
        waiting = (container_status.get("state", {}) or {}).get("waiting", {}) or {}
        reason = waiting.get("reason", "")
        if reason not in IMAGE_PULL_REASONS:
            continue
        container = container_status.get("name", "unknown")
        if found is not None and container != APPLICATION_CONTAINER_NAME:
            continue
        message = f"{reason}: {waiting.get('message', 'image pull failed')}"
        found = ImagePullFailure(
            namespace=metadata.get("namespace", ""),
            pod_name=metadata.get("name", ""),
            app=labels.get("app", ""),
            project=labels.get("project", ""),
            deployment=labels.get("deployment", ""),
            container=container,
            image=container_status.get("image", "") or "",
            message=message,
            reason_class=classify_image_pull_failure(message),
            since=metadata.get("creationTimestamp", ""),
        )
        if container == APPLICATION_CONTAINER_NAME:
            break
    return found


# The whole snapshot is replaced at once, so a reader never sees half of a pass.
_snapshot: tuple[ImagePullFailure, ...] = ()


def image_pull_failures() -> tuple[ImagePullFailure, ...]:
    """Every application pod on this cluster that could not pull, as last observed."""
    return _snapshot


def image_pull_failures_for(namespace: str, apps: set[str]) -> list[ImagePullFailure]:
    """The failures for these component unique names in one namespace, one per component."""
    per_app: dict[str, ImagePullFailure] = {}
    for failure in _snapshot:
        if failure.namespace != namespace or failure.app not in apps:
            continue
        # Several replicas fail the same pull. The component is the unit a reader acts on,
        # so the first pod answers for it.
        per_app.setdefault(failure.app, failure)
    return list(per_app.values())


def image_pull_failure_count() -> int:
    """How many application pods cannot pull, cluster-wide."""
    return len(_snapshot)


def image_pull_failure_counts() -> dict[tuple[str, str], int]:
    """``{(namespace, reason_class): pods}``, for the labelled gauge.

    Namespace times three classes, and never the kubelet message or the image tag: those
    are unbounded and would be a cardinality problem, not a label.
    """
    counts: dict[tuple[str, str], int] = {}
    for failure in _snapshot:
        key = (failure.namespace, failure.reason_class)
        counts[key] = counts.get(key, 0) + 1
    return counts


async def observe_image_pull_failures() -> int:
    """One pass: list the application pods and replace the snapshot. Returns the count.

    One ``kubectl get pods --all-namespaces`` per pass, over the application-pod selector
    that every other read here uses, so a service's own pods (sleep-mode's waker) are not
    attributed to the component they front.

    A failed pass leaves the previous snapshot in place rather than reporting zero: "we
    could not look" is not "nothing is wrong", and zero is what an alert reads as healthy.
    """
    global _snapshot

    kubectl = KubectlConnector()
    if not KubectlConnector.isConnected:
        logger.debug("kubectl not connected, not observing image-pull failures")
        return len(_snapshot)

    args = ["get", "pods", "--all-namespaces", "-l", all_application_pods_selector(), "-o", "json"]
    try:
        stdout, stderr, code = await kubectl.run_command(args)
    except Exception as e:
        logger.warning("Image-pull observation failed, keeping the previous snapshot: %s", e)
        return len(_snapshot)

    if code != 0:
        logger.warning("Image-pull observation failed (%s), keeping the previous snapshot", stderr)
        return len(_snapshot)

    try:
        pods = json.loads(stdout).get("items", []) or []
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning("Unparsable pod list for the image-pull observation: %s", e)
        return len(_snapshot)

    failures = tuple(f for pod in pods if (f := read_image_pull_failure(pod)) is not None)
    previous = len(_snapshot)
    _snapshot = failures
    if failures or previous:
        logger.info(
            "Image-pull observation: %d of %d application pod(s) cannot pull (was %d): %s",
            len(failures),
            len(pods),
            previous,
            ", ".join(f"{f.namespace}/{f.app} [{f.reason_class}]" for f in failures) or "none",
        )
    return len(failures)


class ImagePullObserver:
    """Refreshes the snapshot on an interval, for as long as the process runs.

    Same shape as ``OomMetricSweeper``, and deliberately not behind a per-cluster switch:
    it reads and nothing else, and it is the only thing that now notices a component that
    cannot pull.
    """

    def __init__(self, interval_seconds: int) -> None:
        self._interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="image-pull-observer")
        logger.info("Image-pull observer started (every %ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Image-pull observer stopped")

    async def _run(self) -> None:
        # First pass immediately: a gauge that reads zero for the first interval after a
        # restart is a gauge that says "healthy" about a fleet it has not looked at.
        while self._running:
            try:
                await observe_image_pull_failures()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Image-pull observation failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
