"""De dagelijkse bewaarlus van het audittrail.

Zonder deze lus bewaart ``notification_events`` persoonsgegevens zonder termijn. Daarom
is er geen schakelaar om hem uit te zetten; hij start met de taakwerker mee en draait één
keer per etmaal. De twee passes zelf staan in ``opi/services/audit.py``.
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
    """Draait de bewaarlus één keer per nacht."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start de lus als achtergrondtaak."""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Audit retention scheduler started (nightly at %02d:00 %s)",
            settings.AUDIT_RETENTION_HOUR,
            _TZ.key,
        )

    async def stop(self) -> None:
        """Stop de lus."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Audit retention scheduler stopped")

    async def _run(self) -> None:
        """Slapen tot het nachtelijke uur, één ronde draaien, opnieuw."""
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
        """Eén ronde, met het resultaat in de log."""
        result = await run_retention()
        logger.info(
            "Audit retention: %d rijen gepseudonimiseerd, %d rijen verwijderd",
            result["pseudonymized"],
            result["deleted"],
        )
        return result
