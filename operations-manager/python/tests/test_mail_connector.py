"""The relay connector's HTTP edge: a relay that does not answer must not hang the call."""

from __future__ import annotations

import asyncio
from time import monotonic

import pytest
from opi.connectors import mail
from opi.connectors.mail import MailConnector, MailRelayUnavailableError
from tests.unresponsive_server import unresponsive_server


async def test_an_unresponsive_relay_does_not_hang_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a timeout on the session this waits for aiohttp's own five minutes."""
    monkeypatch.setattr(mail, "RELAY_REQUEST_TIMEOUT_SECONDS", 0.2)

    with unresponsive_server() as url:
        connector = MailConnector(url, "platform", "secret")
        started = monotonic()
        with pytest.raises(MailRelayUnavailableError):
            await asyncio.wait_for(connector.get_principal("someone"), timeout=3)

    assert monotonic() - started < 1


async def test_an_unresponsive_relay_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail, "RELAY_READY_TIMEOUT_SECONDS", 0.2)

    with unresponsive_server() as url:
        assert await MailConnector(url, "platform", "secret").is_ready() is False
