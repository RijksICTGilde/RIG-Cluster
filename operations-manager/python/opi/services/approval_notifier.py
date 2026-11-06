"""Push an ntfy message when a project write introduces a new approval request.

A stopgap until there is a real notification system: it reuses the log-watcher topic,
so the notice lands with whoever already receives the operational alerts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from opi.core.config import settings
from opi.services.approvals import collect_approval_items
from opi.services.catalog.approval import ApprovalStatus
from opi.services.log_watcher import LogWatchConfig, send_ntfy

logger = logging.getLogger(__name__)

# Fire-and-forget tasks need a strong reference, or the loop may collect them mid-send.
_pending: set[asyncio.Task[None]] = set()


def _requested(project_data: dict[str, Any] | None) -> dict[tuple[str, str, str, str], str]:
    if not project_data:
        return {}
    return {
        (item["service"], item["type"], str(item.get("domain", "")), str(item.get("name", ""))): (
            f"{item.get('label', item['type'])}: {item.get('subject', '')}"
        )
        for item in collect_approval_items(project_data)
        if item.get("current_status") == ApprovalStatus.REQUESTED
    }


def notify_new_approval_requests(project: str, before: dict[str, Any] | None, after: dict[str, Any]) -> None:
    """Schedule an ntfy push for requests in ``after`` that were not open in ``before``."""
    if not settings.LOGWATCHER_NTFY_TOPIC:
        return
    old = _requested(before)
    new = [label for key, label in _requested(after).items() if key not in old]
    if not new:
        return
    title = f"ZAD {settings.CLUSTER_MANAGER}: goedkeuring gevraagd door {project}"
    body = "\n".join(new) + "\n\nBeheer > Aanvragen (/admin/approvals)"
    task = asyncio.get_running_loop().create_task(asyncio.to_thread(_post, title, body))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _post(title: str, body: str) -> None:
    cfg = LogWatchConfig(
        grafana_url=settings.GRAFANA_URL,
        ntfy_topic=settings.LOGWATCHER_NTFY_TOPIC or "",
        ntfy_server=settings.LOGWATCHER_NTFY_SERVER,
    )
    with httpx.Client() as client:
        send_ntfy(client, cfg, title, body, "default", "inbox_tray")
