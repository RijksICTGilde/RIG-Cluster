"""The two ways in that the fieldlab registration added to the TransIP connector.

A connector with a ready-made access token must not go to ``/auth``: for the fieldlab zone
there is no private key to sign that call with. And a refusal by TransIP has to be
recognisable as such without the credential ending up in the message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from opi.connectors.transip import TransIPAuthError, TransIPConnector, TransIPError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TOKEN = "nep-fieldlab-token"  # obviously fake, for a local test server
ZONE = "zadlab.nl"


class _Recorder:
    """What the fake TransIP received, so a test can read it back."""

    def __init__(self) -> None:
        self.authorizations: list[str] = []
        self.deleted: list[dict[str, Any]] = []
        self.auth_calls = 0


@pytest.fixture
async def transip() -> AsyncIterator[tuple[TransIPConnector, _Recorder]]:
    recorder = _Recorder()

    async def auth(request: web.Request) -> web.Response:
        recorder.auth_calls += 1
        return web.json_response({"token": "een-ondertekend-token"})

    async def entries(request: web.Request) -> web.Response:
        recorder.authorizations.append(request.headers.get("Authorization", ""))
        if request.method == "DELETE":
            recorder.deleted.append((await request.json())["dnsEntry"])
            return web.Response(status=204)
        return web.json_response({"dnsEntries": [{"name": "*.team42", "type": "A", "content": "1.2.3.4"}]})

    async def forbidden(request: web.Request) -> web.Response:
        return web.json_response({"error": "not whitelisted"}, status=403)

    async def unauthorized(request: web.Request) -> web.Response:
        return web.json_response({"error": "token expired"}, status=401)

    app = web.Application()
    app.router.add_post("/auth", auth)
    app.router.add_get(f"/domains/{ZONE}/dns", entries)
    app.router.add_delete(f"/domains/{ZONE}/dns", entries)
    app.router.add_get("/domains/verboden.nl/dns", forbidden)
    app.router.add_get("/domains/verlopen.nl/dns", unauthorized)
    server = TestServer(app)
    await server.start_server()
    try:
        base_url = str(server.make_url("")).rstrip("/")
        yield TransIPConnector.with_access_token(TOKEN, base_url=base_url), recorder
    finally:
        await server.close()


async def test_an_access_token_is_sent_as_a_bearer_without_signing_in(
    transip: tuple[TransIPConnector, _Recorder],
) -> None:
    connector, recorder = transip

    await connector.get_dns_entries(ZONE)

    assert recorder.authorizations == [f"Bearer {TOKEN}"]
    assert recorder.auth_calls == 0


async def test_an_account_with_a_key_still_signs_in(transip: tuple[TransIPConnector, _Recorder]) -> None:
    """The existing way in keeps working: the token comes from ``/auth``."""
    connector, recorder = transip
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    with_a_key = TransIPConnector("account", pem, base_url=connector.base_url)
    await with_a_key.get_dns_entries(ZONE)

    assert recorder.auth_calls == 1
    assert recorder.authorizations == ["Bearer een-ondertekend-token"]


async def test_deleting_sends_the_whole_record(transip: tuple[TransIPConnector, _Recorder]) -> None:
    connector, recorder = transip

    await connector.delete_dns_entry(ZONE, "*.team42", "A", "1.2.3.4", 300)

    assert recorder.deleted == [{"name": "*.team42", "expire": 300, "type": "A", "content": "1.2.3.4"}]


async def test_a_refused_credential_becomes_its_own_error_without_the_token(
    transip: tuple[TransIPConnector, _Recorder],
) -> None:
    connector, _ = transip

    with pytest.raises(TransIPAuthError) as refused:
        await connector.get_dns_entries("verlopen.nl")

    assert TOKEN not in str(refused.value)
    assert isinstance(refused.value, TransIPError)


async def test_a_forbidden_source_is_the_same_kind_of_error(transip: tuple[TransIPConnector, _Recorder]) -> None:
    """403 means "this source is not whitelisted", which the caller has to tell apart too."""
    connector, _ = transip

    with pytest.raises(TransIPAuthError, match="403"):
        await connector.get_dns_entries("verboden.nl")
