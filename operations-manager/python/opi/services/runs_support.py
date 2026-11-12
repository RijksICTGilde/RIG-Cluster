"""The state of a "run" (a database console, an ad-hoc job) before its pod exists.

Next to :mod:`opi.services.runs_service`, which holds the rows this reads. The manifests
and the apply of a run live with its manager; what a caller needs to SHOW a run that has
not started yet is here, so a service page and a manager read the same verdict.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opi.services.runs_service import RunKind, RunStatus, get_runs_service

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = logging.getLogger(__name__)


def parse_expires(value: str | None) -> datetime | None:
    """Parse a stored expires-at / timestamp annotation; None on missing/bad data."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# A 'starting' run with no pod after this long means provisioning died (e.g. OPI
# restarted mid-start before the pod was applied). The status poll surfaces it as
# a failure so the modal stops spinning, and begin() reconciles it so a stale row
# can never wedge the feature.
STALE_STARTING_SECONDS = 180

# Keep references to background provisioning tasks so they are not garbage
# collected before they finish (asyncio holds only weak references).
_background_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    """Fire-and-forget a coroutine, holding a strong ref until it completes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def is_stale_starting(run: dict[str, Any] | None) -> bool:
    """True if a 'starting' run has outlived the provisioning window.

    The caller must already have confirmed no live pod exists for the run; the
    age of the row then distinguishes a dead start (OPI restarted before the pod
    was applied, so nothing will ever finish it) from one still provisioning.
    begin() uses this to reconcile a stale row instead of rejecting forever.
    """
    if not run or run.get("status") != str(RunStatus.STARTING):
        return False
    started = parse_expires(run.get("started_at"))
    if started is None:
        return False
    return (datetime.now(UTC) - started).total_seconds() > STALE_STARTING_SECONDS


async def pending_state(project_name: str, deployment_name: str, kind: RunKind, subject: str) -> tuple[str, str | None]:
    """Derive modal state from the runs registry when no pod exists yet.

    Returns (state, message) where state is 'none' or 'starting'. A 'starting'
    row past the stale window reports as 'none' with a time-out message so the
    modal stops spinning; a 'failed' row surfaces its error. `subject` is the
    Dutch noun for the workload ("console"/"job") used in fallback messages.
    ``None`` as the message means this registry has nothing to add, and the caller
    keeps the message it already had.
    """
    try:
        run = await get_runs_service().get_latest_run(project_name, deployment_name, kind)
    except Exception:
        logger.exception("Failed to read latest run for %s/%s", project_name, deployment_name)
        return "none", None
    if not run:
        return "none", None
    status = run.get("status")
    if status == "starting":
        if is_stale_starting(run):
            return "none", f"De {subject} kon niet worden gestart (time-out)."
        return "starting", None
    if status == "failed":
        return "none", run.get("error_message") or f"De {subject} kon niet worden gestart."
    return "none", None
