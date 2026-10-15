"""Monitort of de platform-mailrelay ready is en waarschuwt als hij uitvalt."""

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
        self._niet_ready_sinds: float | None = None
        #: Één melding per storing, niet per tik: anders is het kanaal onbruikbaar.
        self._gemeld = False

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
                await self.tik()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in mail relay readiness check")

    async def tik(self) -> None:
        """Een meting, en de melding die eruit volgt."""
        ready = await self._meet()
        if ready is None:
            return

        if ready:
            if self._gemeld:
                await self._stuur(
                    "Mailrelay is weer bereikbaar",
                    "De platform-mailrelay antwoordt weer op /healthz/ready. Uitrollen met send-email werken weer.",
                    priority="default",
                )
            self._niet_ready_sinds = None
            self._gemeld = False
            return

        # De module-eigen naam, zodat een toets de klok kan zetten zonder de event loop te raken.
        nu = monotonic()
        if self._niet_ready_sinds is None:
            self._niet_ready_sinds = nu
            return
        duur = nu - self._niet_ready_sinds
        if duur < settings.MAIL_RELAY_DOWN_ALERT_SECONDS or self._gemeld:
            return
        self._gemeld = True
        await self._stuur(
            "Mailrelay is niet bereikbaar",
            f"De platform-mailrelay is al {duur / 60:.0f} minuten niet ready op "
            f"{settings.MAIL_RELAY_API_URL}. Elke uitrol van een project met send-email faalt zolang dit duurt. "
            "Kijk op CrashLoopBackOff en op security.ip-blocked in de log van de relay "
            "(features/send-email.md, Storingen).",
            priority="high",
        )

    async def _meet(self) -> bool | None:
        """Of de relay ready is, of ``None`` als er niets te meten was.

        ``None`` en niet ``False``: een cluster zonder relay en een wachtwoord dat niet te
        ontsleutelen is zijn geen storing van de relay, en een melding daarover zou naar de
        verkeerde hoek wijzen.
        """
        try:
            connector = await create_mail_connector()
        except MailRelayNotConfiguredError:
            return None
        except ValueError as error:
            logger.warning(f"Mailrelay-readiness niet gemeten: {error}")
            return None
        return await connector.is_ready()

    async def _stuur(self, titel: str, tekst: str, priority: str) -> None:
        if not settings.LOGWATCHER_NTFY_TOPIC:
            logger.warning("Mailrelay-melding niet verstuurd: LOGWATCHER_NTFY_TOPIC ontbreekt (%s)", titel)
            return
        cfg = LogWatchConfig(
            grafana_url=settings.GRAFANA_URL,
            ntfy_topic=settings.LOGWATCHER_NTFY_TOPIC,
            ntfy_server=settings.LOGWATCHER_NTFY_SERVER,
        )
        # send_ntfy is synchroon (httpx), net als in de log_watcher-scheduler.
        await asyncio.to_thread(self._post, cfg, titel, tekst, priority)

    @staticmethod
    def _post(cfg: LogWatchConfig, titel: str, tekst: str, priority: str) -> None:
        with httpx.Client() as client:
            send_ntfy(client, cfg, titel, tekst, priority, "mailbox")
