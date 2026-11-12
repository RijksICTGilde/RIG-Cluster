"""The running sync operation of an ArgoCD Application: read it, judge it, end it.

`.operation` is what BLOCKS the controller, because `autoSync` starts with
`if app.Operation != nil`. `.status.operationState` is what the LAST operation did. The two
part ways the moment someone removes `.operation` while the phase is still `Running`:
nothing cleans up that detached state, so it stays `Running` until a next sync overwrites
it, while auto-sync is already free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opi.connectors.kubectl import KubectlConnectionError, KubectlExecutionError

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)

#: `Terminating` belongs here: the controller sets that phase while the operation still stands.
ONGOING_PHASES = frozenset({"Running", "Terminating"})

#: Equal to OPI's own 300s wait: anything running longer than OPI itself waits for is waiting
#: on something that is not coming. Lower could break off a rollout that is simply still busy.
#:
#: That is for whoever looks at SOMEONE ELSE's rollout: the round for an ordinary push, and
#: the report. Where "still busy" is no longer a possibility, 0 is passed in: on OPI's own
#: time-out branch (``ProjectManager._terminate_operation_we_waited_on``) and on the refresh
#: path, where someone presses reset (``reset_running_sync``).
STUCK_OPERATION_THRESHOLD_SECONDS = 300


@dataclass(frozen=True)
class SyncOperation:
    """What an Application says about its last or running sync."""

    phase: str | None
    started_at: str | None
    finished_at: str | None
    #: `syncResult.revision`: the revision that was actually ROLLED OUT. Not to be confused
    #: with `status.sync.revision`, the revision compared against.
    revision: str | None
    message: str | None
    #: Whether `.operation` is still attached to the Application.
    attached: bool

    @property
    def ongoing(self) -> bool:
        return self.phase in ONGOING_PHASES

    @property
    def blocks_next_sync(self) -> bool:
        return self.attached and self.ongoing


def read_sync_operation(application: dict[str, Any] | None) -> SyncOperation:
    """Read the operation out of an Application payload.

    The same reader for `kubectl get application -o json` and for
    `GET /api/v1/applications/{name}`: argocd-server's swagger gives `v1alpha1Application` the
    same fields as the CR, so `.operation` sits in the same place in both payloads.
    """
    application = application or {}
    state = (application.get("status") or {}).get("operationState") or {}
    sync_result = state.get("syncResult") or {}
    return SyncOperation(
        phase=state.get("phase") or None,
        started_at=state.get("startedAt") or None,
        finished_at=state.get("finishedAt") or None,
        revision=sync_result.get("revision") or None,
        message=state.get("message") or None,
        attached=application.get("operation") is not None,
    )


def describe_sync_waiting(application: dict[str, Any], *, fresh: bool) -> str | None:
    """What ArgoCD itself is still busy with, in plain words, or ``None`` once the sync is done.

    ``None`` means the manifests are on the cluster and whatever is missing sits in the pods.
    """
    if not fresh:
        return "ArgoCD heeft de wijziging nog niet verwerkt"
    operation = read_sync_operation(application)
    if operation.phase == "Terminating":
        return "ArgoCD breekt de lopende sync af"
    if operation.ongoing:
        return f"ArgoCD synchroniseert: {operation.message}" if operation.message else "ArgoCD synchroniseert"
    sync_status = ((application.get("status") or {}).get("sync") or {}).get("status")
    if sync_status != "Synced":
        return f"ArgoCD heeft nog niet gesynchroniseerd ({sync_status or 'Unknown'})"
    return None


def seconds_since(timestamp: str | None, *, now: datetime | None = None) -> int | None:
    """Seconds since an ArgoCD timestamp, or ``None`` when it is absent or unreadable."""
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        logger.warning("Onleesbaar tijdstip van ArgoCD: %r", timestamp)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(((now or datetime.now(UTC)) - moment).total_seconds())


def operation_age_seconds(operation: SyncOperation, *, now: datetime | None = None) -> int | None:
    """How long the operation has been running, or ``None`` without a readable `startedAt`."""
    return seconds_since(operation.started_at, now=now)


def is_stuck(operation: SyncOperation, *, threshold_seconds: int, now: datetime | None = None) -> bool:
    """Whether this operation blocks a next sync and has run at least as long as the threshold.

    An absent or unreadable `startedAt` counts as stuck the moment it blocks: an attached
    operation without a start is exactly the detached state that nothing changes any more.

    A negative age reads as 0, for the same reason as in ``format_duration``: with a clock
    difference between ArgoCD (UTC) and OPI (CEST), `startedAt` lies in the future. Without
    that clamp a threshold of 0 is no longer "always while it blocks", and the blockade that
    has to go would stay on exactly the time-out branch. At a threshold of 300 the clamp
    changes nothing.
    """
    if not operation.blocks_next_sync:
        return False
    age = operation_age_seconds(operation, now=now)
    return age is None or max(age, 0) >= threshold_seconds


async def terminate_stuck_operation(
    kubectl: KubectlConnector,
    app_name: str,
    namespace: str,
    *,
    threshold_seconds: int = STUCK_OPERATION_THRESHOLD_SECONDS,
) -> bool:
    """End the running sync operation of ``app_name`` when it blocks a next sync.

    True when an operation was really removed. False means there was nothing to end (no
    operation, already finished, or still young enough to be simply busy), that the patch
    failed (``terminate_argocd_application_operation`` logs that itself), or that kubectl was
    unreachable.

    That last case is swallowed HERE and not by the caller: this is a best-effort cleanup and
    both callers sit on a path that must not strand. For the push the order is commit, clean
    up, push, so a kubectl that fell over would leave the manifests committed and unpushed; on
    the time-out branch it would turn a clean time-out into a ``KubectlConnectionError``. That
    branch is not rare: ``_run_kubectl_command`` raises as soon as
    ``KubectlConnector.is_connected`` is False, and that flag flips process-wide on a single
    "connection refused", with a background retry of 30s.
    """
    try:
        # No guard of its own for an Application that does not exist: that yields `None`, on
        # which `attached` stays False, so the guard would never get its turn.
        operation = read_sync_operation(await kubectl.get_argocd_application(app_name, namespace))
        if not operation.blocks_next_sync:
            return False

        age = operation_age_seconds(operation)
        if not is_stuck(operation, threshold_seconds=threshold_seconds):
            logger.info(
                "Sync-operation op '%s' loopt %ss (phase %s); onder de drempel van %ss, dus laten staan",
                app_name,
                age,
                operation.phase,
                threshold_seconds,
            )
            return False

        logger.warning(
            "Sync-operation op '%s' loopt %s (phase %s, revisie %s): %s. Die blokkeert elke volgende "
            "sync, dus OPI beëindigt hem",
            app_name,
            f"{age}s" if age is not None else "sinds een onbekend moment",
            operation.phase,
            operation.revision or "onbekend",
            operation.message or "geen bericht",
        )
        return await kubectl.terminate_argocd_application_operation(app_name, namespace)
    except (KubectlConnectionError, KubectlExecutionError) as e:
        logger.warning(
            "Kon de sync-operation op '%s' niet beoordelen of beëindigen: %s. Een eventuele "
            "blokkade blijft staan; de volgende ronde kijkt opnieuw",
            app_name,
            e,
        )
        return False


def format_duration(seconds: int | None) -> str:
    """A runtime as '45 seconden', '19 minuten', '2 uur', or 'onbekend lang'.

    Rounded DOWN, like ``relative_time`` in ``opi/http/render/lotc_switch.py``: 119 minutes
    reads as '1 uur' and not as '2 uur'. A duration and not a clock, because ArgoCD reports in
    UTC and OPI's own lines in CEST. A negative duration comes from that same difference and
    reads as '0 seconden' instead of an impossible number.
    """
    if seconds is None:
        return "onbekend lang"
    seconds = max(seconds, 0)
    if seconds < 60:
        return "1 seconde" if seconds == 1 else f"{seconds} seconden"
    minutes = seconds // 60
    if minutes < 60:
        return "1 minuut" if minutes == 1 else f"{minutes} minuten"
    hours = minutes // 60
    return "1 uur" if hours == 1 else f"{hours} uur"
