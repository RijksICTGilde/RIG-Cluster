"""The ntfy push: what the server receives, and when the caller is told it failed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from opi.connectors import ntfy
from opi.connectors.ntfy import NtfyMessage, NtfyTarget, send_ntfy

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock


def _target() -> NtfyTarget:
    return NtfyTarget(server="https://ntfy.example/", topic="topic-xyz")


def test_push_carries_title_priority_and_tags(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://ntfy.example/topic-xyz")

    assert send_ntfy(_target(), NtfyMessage("a title", "a body", "high", "mag")) is True
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["Title"] == "a title"
    assert request.headers["Priority"] == "high"
    assert request.headers["Tags"] == "mag"
    assert request.read() == b"a body"


def test_non_ascii_title_is_stripped(httpx_mock: HTTPXMock) -> None:
    """ntfy reads the Title header as ASCII, so anything else would make it reject the push."""
    httpx_mock.add_response(url="https://ntfy.example/topic-xyz")

    send_ntfy(_target(), NtfyMessage("ruimte vrijgemaakt één keer", "body"))

    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["Title"] == "ruimte vrijgemaakt n keer"


def test_unreachable_server_is_reported_as_not_sent(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("no route to host"))

    assert send_ntfy(_target(), NtfyMessage("a title", "a body")) is False


@pytest.mark.parametrize("status", [404, 500])
def test_refused_push_is_reported_as_not_sent(httpx_mock: HTTPXMock, status: int) -> None:
    httpx_mock.add_response(url="https://ntfy.example/topic-xyz", status_code=status)

    assert send_ntfy(_target(), NtfyMessage("a title", "a body")) is False


def test_notify_pushes_on_the_configured_topic(httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ntfy.settings, "LOGWATCHER_NTFY_SERVER", "https://ntfy.example")
    monkeypatch.setattr(ntfy.settings, "LOGWATCHER_NTFY_TOPIC", "topic-xyz")
    httpx_mock.add_response(url="https://ntfy.example/topic-xyz")

    assert ntfy.notify("a title", "a body", "default", "mag") is True


def test_notify_without_a_topic_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ntfy.settings, "LOGWATCHER_NTFY_TOPIC", None)

    assert ntfy.notify("a title", "a body", "default", "mag") is False
