"""Push notifications over ntfy: the one place that knows how a push is shaped.

Synchronous (httpx), because the log-watcher pipeline that sends most of them is
synchronous too. Async callers reach it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from opi.core.config import settings

logger = logging.getLogger(__name__)

NTFY_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class NtfyTarget:
    server: str
    topic: str


@dataclass(frozen=True)
class NtfyMessage:
    title: str
    body: str
    priority: str = "default"
    tags: str = ""


def send_ntfy(target: NtfyTarget, message: NtfyMessage) -> bool:
    """POST one push; False when ntfy refused it or could not be reached."""
    server = target.server.rstrip("/")
    # Multi-line body logs as separate Loki entries lacking module context, causing
    # the watcher to re-ingest its output. Log one-line summaries instead.
    body = message.body
    logger.info(
        "ntfy push [%s] %s (%d problem line(s))",
        message.priority,
        message.title,
        body.count("\n\n") + 1 if body else 0,
    )
    try:
        with httpx.Client() as client:
            response = client.post(
                f"{server}/{target.topic}",
                content=body.encode("utf-8"),
                headers={
                    "Title": message.title.encode("ascii", "ignore").decode(),
                    "Priority": message.priority,
                    "Tags": message.tags,
                },
                timeout=NTFY_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("ntfy POST failed: %s", e)
        return False
    logger.info("ntfy sent to %s/%s (priority %s)", server, target.topic, message.priority)
    return True


def notify(title: str, body: str, priority: str, tags: str) -> bool:
    """One push on the platform's own topic, read from the settings.

    False and a warning when no topic is configured: a cluster without ntfy stays
    silent instead of posting to the root of the server.
    """
    if not settings.LOGWATCHER_NTFY_TOPIC:
        logger.warning("ntfy push not sent, LOGWATCHER_NTFY_TOPIC is missing: %s", title)
        return False
    target = NtfyTarget(settings.LOGWATCHER_NTFY_SERVER, settings.LOGWATCHER_NTFY_TOPIC)
    return send_ntfy(target, NtfyMessage(title, body, priority, tags))
