"""The daily retention loop of the audit trail.

Without this loop, ``notification_events`` keeps personal data without a term. That is why
there is no switch to turn it off; it starts along with the task worker and runs once every
24 hours. The two passes themselves live in ``opi/services/audit.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from opi.core.actor import scheduler_actor, set_actor
from opi.core.config import settings
from opi.core.reconciliation_scheduler import _TZ, _seconds_until_next_run
from opi.services.audit import run_retention

logger = logging.getLogger(__name__)


class AuditRetentionScheduler:
    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Audit retention scheduler started (nightly at %02d:00 %s)",
            settings.AUDIT_RETENTION_HOUR,
            _TZ.key,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Audit retention scheduler stopped")

    async def _run(self) -> None:
        """Sleep until the nightly hour, run one round, again."""
        set_actor(scheduler_actor("audit-retention"))
        while self._running:
            try:
                await asyncio.sleep(_seconds_until_next_run(settings.AUDIT_RETENTION_HOUR, datetime.now(_TZ)))
            except asyncio.CancelledError:
                break

            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in nightly audit retention run")

    async def run_once(self) -> dict[str, int]:
        """One round, with the result in the log."""
        result = await run_retention()
        logger.info(
            "Audit retention: %d rijen gepseudonimiseerd, %d rijen verwijderd",
            result["pseudonymized"],
            result["deleted"],
        )
        return result
