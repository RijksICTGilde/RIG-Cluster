"""The ntfy push for new approval requests: only a request that was not open before."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opi.services import approval_notifier
from opi.services.services_enums import ServiceType


def _project(status: str | None) -> dict[str, Any]:
    services: list[Any] = []
    if status is not None:
        services.append(
            {"name": ServiceType.SEND_EMAIL.value, "config": {"approval": {"status": status, "history": []}}}
        )
    return {"name": "myproject", "services": services}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(approval_notifier.settings, "LOGWATCHER_NTFY_TOPIC", "topic-xyz")
    monkeypatch.setattr(approval_notifier, "_post", lambda title, body: calls.append((title, body)))
    return calls


async def _notify(before: dict[str, Any] | None, after: dict[str, Any]) -> None:
    approval_notifier.notify_new_approval_requests("myproject", before, after)
    await asyncio.gather(*approval_notifier._pending)


async def test_new_request_notifies(sent: list[tuple[str, str]]) -> None:
    await _notify(_project(None), _project("requested"))
    assert len(sent) == 1
    assert "myproject" in sent[0][0]


async def test_already_open_request_does_not_notify_again(sent: list[tuple[str, str]]) -> None:
    await _notify(_project("requested"), _project("requested"))
    assert sent == []


async def test_verdict_does_not_notify(sent: list[tuple[str, str]]) -> None:
    await _notify(_project("requested"), _project("approved"))
    assert sent == []


async def test_new_project_with_request_notifies(sent: list[tuple[str, str]]) -> None:
    await _notify(None, _project("requested"))
    assert len(sent) == 1


async def test_without_topic_nothing_is_sent(monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str]]) -> None:
    monkeypatch.setattr(approval_notifier.settings, "LOGWATCHER_NTFY_TOPIC", None)
    await _notify(_project(None), _project("requested"))
    assert sent == []
