"""Which application pods cannot pull their image, as a live picture of the cluster.

One observation feeds both the gauges in ``opi/core/metrics.py`` and the deployment card.
Since RC-243 nothing switches such a component off any more, so this is the only thing
that notices one: see ``features/image-pull-backoff-detection.md``.

A periodic LIST, not the pod watch of ``oom_watcher``. Two reasons, and the second is the
one that decides it: the watch is off on every cluster until it has proven itself
(``watches_pods_for_oom``), so it is not something to build on today; and a snapshot that
is recounted from scratch cannot carry a stale entry, while a stream that misses a DELETED
would keep alerting on a pod that is long gone.

What the LIST does not buy is the right to read the cluster. It is cluster-wide
(``--all-namespaces``), and on ``odcn-production`` OPI reaches pods through Capsule Proxy
under the per-namespace RoleBinding Capsule makes; whether a cluster-wide pod read gets
through there is NOT measured. That is the same open point as for the watch and for the
same reason -- a watch uses the same rights as a list, see ``features/oom-pod-watch.md``.
So this cannot claim to work everywhere. It can only make it visible where it does not,
which is what ``_observed_at`` below is for.

The snapshot is process-local. Nothing reads it to decide anything: it is a report.
"""

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass

from opi.connectors.kubectl import KubectlConnector
from opi.handlers.project_file_handler import classify_image_pull_failure, read_image_pull_from_statuses
from opi.services.catalog.base import all_application_pods_selector, is_application_pod

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
    #: Which container failed (see ``read_image_pull_from_statuses`` for the precedence).
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

    pull = read_image_pull_from_statuses((pod.get("status", {}) or {}).get("containerStatuses", []) or [])
    if pull is None:
        return None
    return ImagePullFailure(
        namespace=metadata.get("namespace", ""),
        pod_name=metadata.get("name", ""),
        app=labels.get("app", ""),
        project=labels.get("project", ""),
        deployment=labels.get("deployment", ""),
        container=pull.container,
        image=pull.image,
        message=pull.message,
        reason_class=classify_image_pull_failure(pull.message),
        since=metadata.get("creationTimestamp", ""),
    )


# The whole snapshot is replaced at once, so a reader never sees half of a pass.
_snapshot: tuple[ImagePullFailure, ...] = ()

# When the last pass that actually read the cluster finished, as a unix timestamp. 0 means
# no pass has ever succeeded, which is NOT the same as a healthy fleet: the snapshot starts
# empty, so without this a cluster where the read is refused reports a count of zero and
# every reader of that count calls it healthy. Exposed as a gauge and alerted on, because
# "we could not look" is the one failure this module cannot report as a failing pod.
_observed_at: float = 0.0


def image_pull_observed_timestamp() -> float:
    """Unix timestamp of the last observation that reached the cluster (0.0: never)."""
    return _observed_at


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
    It also leaves ``_observed_at`` alone, which is what makes such a pass visible at all:
    before the first success there is no previous snapshot to keep, so the count a failing
    pass reports is an honest zero and an indistinguishable one.
    """
    global _observed_at, _snapshot

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
    _observed_at = time.time()
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
