"""The hook lego runs for a DNS-01 challenge: what it sends, and where the token comes from.

The hook is a process of its own, so its only input is its arguments plus the environment
it inherits. A token in an argument would be readable in ``ps``, so it has to come from the
environment and nowhere else.

The last two tests run it the way lego does, as a process with exactly the environment
``certificate._environment`` builds. Patched settings in this process cannot show that: the
hook reads them back out of that environment, so a guard over a setting that does not
travel there passes here and refuses in the pod.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from opi.connectors.transip import TRANSIP_API_BASE, TransIPError
from opi.core.config import settings
from opi.fundament_registration import certificate, configuration, dns_hook
from opi.fundament_registration.configuration import FundamentConfiguration

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

TOKEN = "nep-hook-token"
ZONE = "zadlab.nl"
FQDN = "_acme-challenge.team42.zadlab.nl."
VALUE = "een-uitdaging"
RECORD = {"name": "_acme-challenge.team42", "expire": 300, "type": "TXT", "content": VALUE}

#: What an attempt runs on. The hook gets the two of these that travel in its environment.
CONFIG = FundamentConfiguration(
    transip_token=TOKEN,
    zone=ZONE,
    acme_directory="https://acme-staging-v02.api.letsencrypt.org/directory",
    acme_email="fieldlab@voorbeeld.nl",
    sso_client_id="zad-fieldlab",
    sso_client_secret="nep-client-secret",
    sso_discovery_url="https://keycloak.voorbeeld.nl/realms/rig-platform/.well-known/openid-configuration",
)

#: Budget for one hook process: it starts a Python and does one call to a server next door.
HOOK_TIMEOUT_SECONDS = 120


@pytest.fixture
def configured() -> Iterator[None]:
    """The two settings the hook reads back from the environment lego hands it.

    No more than two on purpose: patching the five an attempt needs would hide a guard that
    asks for them.
    """
    with (
        patch.object(settings, "FUNDAMENT_REGISTRATION_TRANSIP_TOKEN", TOKEN),
        patch.object(settings, "FUNDAMENT_REGISTRATION_ZONE", ZONE),
    ):
        yield


@pytest.fixture
def connector() -> Iterator[Any]:
    """A fake TransIP, with the token it was built with readable on it."""
    fake = AsyncMock()
    with patch.object(dns_hook.TransIPConnector, "with_access_token", return_value=fake) as factory:
        fake.factory = factory
        yield fake


def test_a_challenge_name_is_relative_to_the_zone() -> None:
    assert dns_hook.record_name(FQDN, ZONE) == "_acme-challenge.team42"


def test_the_zone_itself_is_the_apex() -> None:
    assert dns_hook.record_name("zadlab.nl.", ZONE) == "@"


def test_a_name_outside_the_zone_is_refused() -> None:
    """Without this the hook would write in a zone this token was not handed over for."""
    with pytest.raises(ValueError, match="ligt niet in zone"):
        dns_hook.record_name("_acme-challenge.team42.elders.nl", ZONE)


async def test_present_adds_the_txt_record_with_the_token_from_the_environment(
    configured: None, connector: Any
) -> None:
    assert await dns_hook.run(["present", FQDN, VALUE]) == 0

    connector.factory.assert_called_once_with(TOKEN, base_url=TRANSIP_API_BASE)
    connector.add_dns_entry.assert_awaited_once_with(ZONE, "_acme-challenge.team42", "TXT", VALUE, 300)


async def test_cleanup_removes_the_same_record(configured: None, connector: Any) -> None:
    assert await dns_hook.run(["cleanup", FQDN, VALUE]) == 0

    connector.delete_dns_entry.assert_awaited_once_with(ZONE, "_acme-challenge.team42", "TXT", VALUE, 300)


async def test_a_failure_at_transip_is_a_non_zero_exit(configured: None, connector: Any) -> None:
    """lego has to stop on this, instead of waiting for a record that will never be there."""
    connector.add_dns_entry.side_effect = TransIPError("de zone is niet van dit account")

    assert await dns_hook.run(["present", FQDN, VALUE]) == 1


async def test_a_wrong_call_does_not_touch_dns(configured: None, connector: Any) -> None:
    assert await dns_hook.run(["present", FQDN]) == 2

    connector.add_dns_entry.assert_not_awaited()


async def test_without_the_token_the_hook_refuses(connector: Any) -> None:
    with patch.object(settings, "FUNDAMENT_REGISTRATION_TRANSIP_TOKEN", None):
        assert await dns_hook.run(["present", FQDN, VALUE]) == 3

    connector.add_dns_entry.assert_not_awaited()


class _Recorder:
    """What the fake TransIP received from the hook process."""

    def __init__(self) -> None:
        self.authorizations: list[str] = []
        self.added: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []


@pytest.fixture
async def fake_transip() -> AsyncIterator[tuple[str, _Recorder]]:
    """A TransIP of our own on a local port, so no call leaves this machine."""
    recorder = _Recorder()

    async def entries(request: web.Request) -> web.Response:
        recorder.authorizations.append(request.headers.get("Authorization", ""))
        entry = (await request.json())["dnsEntry"]
        if request.method == "DELETE":
            recorder.deleted.append(entry)
        else:
            recorder.added.append(entry)
        return web.Response(status=201 if request.method == "POST" else 204)

    app = web.Application()
    app.router.add_post(f"/domains/{ZONE}/dns", entries)
    app.router.add_delete(f"/domains/{ZONE}/dns", entries)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/"), recorder
    finally:
        await server.close()


async def _as_lego_runs_it(path: Path, base_url: str, action: str) -> tuple[int, str]:
    """One hook call as a process, in exactly the environment lego hands it.

    The working directory is the temporary one, so no ``.env`` next to the code fills in a
    setting that the pod would not have either.
    """
    certificate._write_hook(path)
    with patch.object(configuration, "TRANSIP_API_BASE", base_url):
        environment = certificate._environment(path, CONFIG)

    process = await asyncio.create_subprocess_exec(
        str(path / certificate.HOOK_NAME),
        action,
        FQDN,
        VALUE,
        env=environment,
        cwd=str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=HOOK_TIMEOUT_SECONDS)
    return process.returncode or 0, stderr.decode(errors="replace")


async def test_the_hook_sets_the_record_in_the_environment_lego_gives_it(
    tmp_path: Path, fake_transip: tuple[str, _Recorder]
) -> None:
    """Measured as a process: this is the run that has to put the TXT record there."""
    base_url, recorder = fake_transip

    code, stderr = await _as_lego_runs_it(tmp_path, base_url, dns_hook.PRESENT)

    assert code == 0, stderr
    assert recorder.added == [RECORD]
    assert recorder.authorizations == [f"Bearer {TOKEN}"]


async def test_the_hook_removes_the_record_in_that_same_environment(
    tmp_path: Path, fake_transip: tuple[str, _Recorder]
) -> None:
    base_url, recorder = fake_transip

    code, stderr = await _as_lego_runs_it(tmp_path, base_url, dns_hook.CLEANUP)

    assert code == 0, stderr
    assert recorder.deleted == [RECORD]
