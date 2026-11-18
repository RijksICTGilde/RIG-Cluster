"""The attempt behind a registration: the A record, the certificate, and what is stored.

Everything outside this process is faked: there is no TransIP and no Let's Encrypt here.
What is measured is which calls the attempt makes, what ends up in the table, and what
never ends up in the log.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.transip import TransIPError
from opi.core.config import settings
from opi.core.db import session_scope
from opi.fundament_registration import registration, store
from opi.fundament_registration.certificate import CertificateError, IssuedCertificate
from opi.fundament_registration.configuration import FundamentConfiguration
from opi.fundament_registration.store import FundamentRegistration, State, Step
from opi.utils.age import generate_age_key_pair

if TYPE_CHECKING:
    from collections.abc import Iterator

TEAM = "team42"
CLUSTER = "fundament-1"
IP = "194.135.48.131"
OTHER_IP = "194.135.48.132"
ZONE = "zadlab.nl"
TOKEN = "nep-transip-token"
CLIENT_SECRET = "nep-client-secret"

CERTIFICATE = "-----BEGIN CERTIFICATE-----\nnep-certificaat\n-----END CERTIFICATE-----\n"
PRIVATE_KEY = "-----BEGIN EC PRIVATE KEY-----\nnep-sleutel\n-----END EC PRIVATE KEY-----\n"

CONFIG = FundamentConfiguration(
    transip_token=TOKEN,
    zone=ZONE,
    acme_directory="https://acme-staging-v02.api.letsencrypt.org/directory",
    acme_email="fieldlab@voorbeeld.nl",
    sso_client_id="zad-fieldlab",
    sso_client_secret=CLIENT_SECRET,
    sso_discovery_url="https://keycloak.voorbeeld.nl/realms/rig-platform/.well-known/openid-configuration",
)


@pytest.fixture
def platform_key() -> Iterator[str]:
    """A throwaway platform key, so the stored private key can be opened again here."""
    private_key, public_key = generate_age_key_pair()
    with (
        patch.object(settings, "SOPS_AGE_PUBLIC_KEY", public_key),
        patch.object(settings, "SOPS_AGE_PRIVATE_KEY", private_key),
    ):
        yield private_key


@pytest.fixture
def transip() -> Iterator[Any]:
    """A fake TransIP that holds one existing A record for this team."""
    fake = AsyncMock()
    fake.get_dns_entries.return_value = [
        {"name": f"*.{TEAM}", "type": "A", "content": "198.51.100.9", "expire": 300},
        {"name": "@", "type": "A", "content": "198.51.100.1", "expire": 300},
    ]
    with patch.object(registration.TransIPConnector, "with_access_token", return_value=fake):
        yield fake


@pytest.fixture
def lego() -> Iterator[AsyncMock]:
    """A fake issuance, so no test ever talks to Let's Encrypt."""
    issued = AsyncMock(return_value=IssuedCertificate(chain=CERTIFICATE, private_key=PRIVATE_KEY))
    with patch.object(registration, "obtain_certificate", issued):
        yield issued


async def _claimed() -> None:
    assert await store.claim_first_attempt(TEAM, CLUSTER, IP)


async def _stored_private_key() -> str | None:
    """The column as it sits in the table, without opening it."""
    async with session_scope() as session:
        row = await session.get(FundamentRegistration, TEAM)
    return None if row is None else row.private_key


async def test_an_attempt_sets_the_record_and_stores_the_certificate(
    orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock
) -> None:
    await _claimed()

    await registration.run_attempt(TEAM, IP, CONFIG)

    transip.delete_dns_entry.assert_awaited_once_with(ZONE, f"*.{TEAM}", "A", "198.51.100.9", 300)
    transip.add_dns_entry.assert_awaited_once_with(ZONE, f"*.{TEAM}", "A", IP, 300)
    lego.assert_awaited_once_with(f"*.{TEAM}.{ZONE}", CONFIG)
    row = await store.read(TEAM)
    assert row is not None
    assert (row.state, row.step) == (State.DONE, None)
    assert row.certificate == CERTIFICATE
    assert row.private_key == PRIVATE_KEY


async def test_the_private_key_sits_encrypted_in_the_table(
    orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock
) -> None:
    """A database backup must not carry a usable key."""
    await _claimed()

    await registration.run_attempt(TEAM, IP, CONFIG)

    stored = await _stored_private_key()
    assert stored is not None
    assert stored.startswith("base64+age:")
    assert "BEGIN" not in stored
    assert "nep-sleutel" not in stored


async def test_a_failing_record_stops_before_the_certificate(
    orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock
) -> None:
    await _claimed()
    transip.add_dns_entry.side_effect = TransIPError("de zone hoort niet bij dit account")

    await registration.run_attempt(TEAM, IP, CONFIG)

    lego.assert_not_awaited()
    row = await store.read(TEAM)
    assert row is not None
    assert (row.state, row.step) == (State.FAILED, Step.DNS)


async def test_a_failing_issuance_is_recorded_as_the_certificate_step(
    orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock
) -> None:
    await _claimed()
    lego.side_effect = CertificateError("acme: authorization error")

    await registration.run_attempt(TEAM, IP, CONFIG)

    row = await store.read(TEAM)
    assert row is not None
    assert (row.state, row.step) == (State.FAILED, Step.CERTIFICATE)


async def test_a_new_address_moves_the_record_and_keeps_the_certificate(
    orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock
) -> None:
    """A reinstall on the same cluster gets the same certificate, also on another address."""
    await _claimed()
    await registration.run_attempt(TEAM, IP, CONFIG)
    lego.reset_mock()
    transip.add_dns_entry.reset_mock()
    transip.delete_dns_entry.reset_mock()

    answer = await registration.handle(TEAM, CLUSTER, OTHER_IP, CONFIG)

    assert answer.outcome is registration.Outcome.READY
    assert answer.registration is not None
    assert answer.registration.certificate == CERTIFICATE
    lego.assert_not_awaited()
    transip.add_dns_entry.assert_awaited_once_with(ZONE, f"*.{TEAM}", "A", OTHER_IP, 300)
    row = await store.read(TEAM)
    assert row is not None
    assert row.ip == OTHER_IP


async def test_the_same_address_leaves_dns_alone(orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock) -> None:
    await _claimed()
    await registration.run_attempt(TEAM, IP, CONFIG)
    transip.add_dns_entry.reset_mock()

    answer = await registration.handle(TEAM, CLUSTER, IP, CONFIG)

    assert answer.outcome is registration.Outcome.READY
    transip.add_dns_entry.assert_not_awaited()


#: What may never show up in a log line, whatever happens.
FORBIDDEN = (TOKEN, CLIENT_SECRET, "BEGIN", "nep-sleutel", "nep-certificaat")


async def test_a_successful_attempt_logs_no_secret(
    orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    await _claimed()

    with caplog.at_level(logging.DEBUG):
        await registration.run_attempt(TEAM, IP, CONFIG)

    assert TEAM in caplog.text
    for secret in FORBIDDEN:
        assert secret not in caplog.text


async def test_a_failed_attempt_logs_no_secret(
    orm_db: Any, platform_key: str, transip: Any, lego: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure is the moment a text from outside is copied into a log line."""
    await _claimed()
    lego.side_effect = CertificateError(f"acme gaf iets terug met {TOKEN} erin")

    with caplog.at_level(logging.DEBUG):
        await registration.run_attempt(TEAM, IP, CONFIG)

    for secret in FORBIDDEN:
        assert secret not in caplog.text
