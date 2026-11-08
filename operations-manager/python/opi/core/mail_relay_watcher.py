"""Watches whether the platform mail relay is ready, and warns when it goes down."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from time import monotonic

import httpx

from opi.connectors.mail import MailRelayNotConfiguredError, create_mail_connector
from opi.core.config import settings
from opi.services.log_watcher import LogWatchConfig, send_ntfy

logger = logging.getLogger(__name__)


class MailRelayWatcher:
    """Polls the relay's own readiness endpoint and notifies on a lasting outage."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._not_ready_since: float | None = None
        #: One message per outage, not per tick: otherwise the channel is unusable.
        self._reported = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Mail relay watcher started (every %ds, alert after %ds not ready)",
            settings.MAIL_RELAY_CHECK_INTERVAL_SECONDS,
            settings.MAIL_RELAY_DOWN_ALERT_SECONDS,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Mail relay watcher stopped")

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(settings.MAIL_RELAY_CHECK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in mail relay readiness check")

    async def tick(self) -> None:
        """One measurement, and the message that follows from it."""
        ready = await self._measure()
        if ready is None:
            return

        if ready:
            if self._reported:
                await self._send(
                    "Mailrelay is weer bereikbaar",
                    "De platform-mailrelay antwoordt weer op /healthz/ready. Uitrollen met send-email werken weer.",
                    priority="default",
                )
            self._not_ready_since = None
            self._reported = False
            return

        # The module's own name for the clock, so a test can set it without touching the
        # event loop.
        now = monotonic()
        if self._not_ready_since is None:
            self._not_ready_since = now
            return
        duration = now - self._not_ready_since
        if duration < settings.MAIL_RELAY_DOWN_ALERT_SECONDS or self._reported:
            return
        self._reported = True
        await self._send(
            "Mailrelay is niet bereikbaar",
            f"De platform-mailrelay is al {duration / 60:.0f} minuten niet ready op "
            f"{settings.MAIL_RELAY_API_URL}. Elke uitrol van een project met send-email faalt zolang dit duurt. "
            "Kijk op CrashLoopBackOff en op security.ip-blocked in de log van de relay "
            "(features/send-email.md, Storingen).",
            priority="high",
        )

    async def _measure(self) -> bool | None:
        """Whether the relay is ready, or ``None`` when there was nothing to measure.

        ``None`` and not ``False``: a cluster without a relay, and a password that cannot be
        decrypted, are not an outage of the relay, and a message about it would point at the
        wrong corner.
        """
        try:
            connector = await create_mail_connector()
        except MailRelayNotConfiguredError:
            return None
        except ValueError as error:
            logger.warning(f"Mailrelay-readiness niet gemeten: {error}")
            return None
        return await connector.is_ready()

    async def _send(self, title: str, text: str, priority: str) -> None:
        if not settings.LOGWATCHER_NTFY_TOPIC:
            logger.warning("Mailrelay-melding niet verstuurd: LOGWATCHER_NTFY_TOPIC ontbreekt (%s)", title)
            return
        cfg = LogWatchConfig(
            grafana_url=settings.GRAFANA_URL,
            ntfy_topic=settings.LOGWATCHER_NTFY_TOPIC,
            ntfy_server=settings.LOGWATCHER_NTFY_SERVER,
        )
        # send_ntfy is synchronous (httpx), the same as in the log_watcher scheduler.
        await asyncio.to_thread(self._post, cfg, title, text, priority)

    @staticmethod
    def _post(cfg: LogWatchConfig, title: str, text: str, priority: str) -> None:
        with httpx.Client() as client:
            send_ntfy(client, cfg, title, text, priority, "mailbox")
