"""The contract of ``POST /api/fundament/registreer``, row by row.

The table in ``features/fundament-registratie.md`` is the agreement with the Fundament
installer: both sides build against it, so every row has a test here. The attempt itself
runs behind the answer and is measured in ``test_fundament_registratie_aanvraag.py``; here
it is only counted, so what is measured is the decision and not the issuance.

The requests go through an ASGI transport and not through ``TestClient``: that one runs the
application in an event loop of its own, and the database connection of this test belongs
to this loop.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from opi.core.config import settings
from opi.core.db import session_scope
from opi.fundament_registration import registration, store
from opi.fundament_registration.store import FundamentRegistration, State, Step
from opi.server import create_app
from opi.utils.age import generate_age_key_pair
from sqlalchemy import update

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

CODE = "NEP-FIELDLAB-CODE"
TEAM = "team42"
CLUSTER = "3f9c2a7e-0b1d-4c55-9e1a-2b7d8c4f6a10"
IP = "194.135.48.131"
ZONE = "zadlab.nl"
CLIENT_SECRET = "nep-client-secret"
PREAUTH_KEY = "nep-vlam-preauth-key"
VLAM_API_KEY = "nep-vlam-api-key"
DISCOVERY = "https://keycloak.voorbeeld.nl/realms/rig-platform/.well-known/openid-configuration"
PATH = "/api/fundament/registreer"

CERTIFICATE = "-----BEGIN CERTIFICATE-----\nnep\n-----END CERTIFICATE-----\n"
PRIVATE_KEY = "-----BEGIN EC PRIVATE KEY-----\nnep\n-----END EC PRIVATE KEY-----\n"

QUESTION = {"code": CODE, "team": TEAM, "cluster_id": CLUSTER, "ip": IP}

SETTINGS: dict[str, Any] = {
    "FUNDAMENT_REGISTRATION_CODE": CODE,
    "FUNDAMENT_REGISTRATION_TRANSIP_TOKEN": "nep-transip-token",
    "FUNDAMENT_REGISTRATION_ZONE": ZONE,
    "FUNDAMENT_REGISTRATION_ACME_DIRECTORY": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "FUNDAMENT_REGISTRATION_ACME_EMAIL": "fieldlab@voorbeeld.nl",
    "FUNDAMENT_REGISTRATION_SSO_CLIENT_ID": "zad-fieldlab",
    "FUNDAMENT_REGISTRATION_SSO_CLIENT_SECRET": CLIENT_SECRET,
    "FUNDAMENT_REGISTRATION_SSO_DISCOVERY_URL": DISCOVERY,
}


def _applied(stack: ExitStack, **overrides: Any) -> None:
    """Put the settings of a cluster that takes part in the fieldlab on this stack.

    The platform key is generated here, so no working key sits in the tree and what the
    table stores can still be opened again.
    """
    private_key, public_key = generate_age_key_pair()
    values = {
        **SETTINGS,
        "SOPS_AGE_PUBLIC_KEY": public_key,
        "SOPS_AGE_PRIVATE_KEY": private_key,
        **overrides,
    }
    for name, value in values.items():
        stack.enter_context(patch.object(settings, name, value))


class _Spawns:
    """The attempts a question started, instead of running them."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    def __call__(self, team: str, ip: str, config: Any) -> None:
        self.started.append((team, ip))


@pytest.fixture
def spawns() -> Iterator[_Spawns]:
    recorder = _Spawns()
    with patch.object(registration, "_spawn", recorder):
        yield recorder


#: The two secrets that put the optional ``vlam`` field in the answer.
VLAM_SECRETS: dict[str, Any] = {
    "FUNDAMENT_REGISTRATION_VLAM_PREAUTH_KEY": PREAUTH_KEY,
    "FUNDAMENT_REGISTRATION_VLAM_API_KEY": VLAM_API_KEY,
}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A caller against an application that has the endpoint, fully configured."""
    with ExitStack() as stack:
        _applied(stack)
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as caller:
            yield caller


async def _running_row() -> None:
    """The row a first question leaves behind: an attempt that is under way."""
    assert await store.claim_first_attempt(TEAM, CLUSTER, IP)


async def _age_the_row(minutes: int) -> None:
    """Move the row back in time, the way a pod that restarted leaves it."""
    moved = datetime.now(UTC) - timedelta(minutes=minutes)
    async with session_scope() as session:
        await session.execute(
            update(FundamentRegistration).where(FundamentRegistration.team == TEAM).values(updated_at=moved)
        )


async def test_an_unknown_team_starts_an_attempt_and_is_told_to_come_back(
    orm_db: Any, client: AsyncClient, spawns: _Spawns
) -> None:
    answer = await client.post(PATH, json=QUESTION)

    assert answer.status_code == 202
    assert answer.json() == {"status": "bezig"}
    assert answer.headers["Retry-After"] == "10"
    assert spawns.started == [(TEAM, IP)]
    row = await store.read(TEAM)
    assert row is not None
    assert (row.cluster_id, row.state, row.step) == (CLUSTER, State.BUSY, Step.DNS)


async def test_a_running_attempt_is_told_to_come_back_without_a_second_attempt(
    orm_db: Any, client: AsyncClient, spawns: _Spawns
) -> None:
    await _running_row()

    answer = await client.post(PATH, json=QUESTION)

    assert answer.status_code == 202
    assert spawns.started == []


async def test_two_questions_at_once_start_one_attempt(orm_db: Any, client: AsyncClient, spawns: _Spawns) -> None:
    """The claim is a conditional write, so the second question loses it and starts nothing."""
    first, second = await asyncio.gather(client.post(PATH, json=QUESTION), client.post(PATH, json=QUESTION))

    assert (first.status_code, second.status_code) == (202, 202)
    assert spawns.started == [(TEAM, IP)]


async def test_another_cluster_may_not_take_the_name(orm_db: Any, client: AsyncClient, spawns: _Spawns) -> None:
    await _running_row()
    await store.mark_done(TEAM, CERTIFICATE, PRIVATE_KEY)

    answer = await client.post(PATH, json={**QUESTION, "cluster_id": "een-ander-cluster"})

    assert answer.status_code == 409
    assert answer.json() == {"detail": "Deze teamnaam is al in gebruik, kies een andere"}


async def test_another_cluster_is_refused_while_the_first_attempt_is_still_running(
    orm_db: Any, client: AsyncClient, spawns: _Spawns
) -> None:
    await _running_row()

    answer = await client.post(PATH, json={**QUESTION, "cluster_id": "een-ander-cluster"})

    assert answer.status_code == 409


async def test_another_cluster_is_refused_after_a_failed_attempt(
    orm_db: Any, client: AsyncClient, spawns: _Spawns
) -> None:
    """A name stays with the cluster that asked for it first, failure or not."""
    await _running_row()
    await store.mark_failed(TEAM, Step.DNS)

    answer = await client.post(PATH, json={**QUESTION, "cluster_id": "een-ander-cluster"})

    assert answer.status_code == 409


async def test_a_finished_registration_hands_back_the_certificate_and_the_sso_details(
    orm_db: Any, client: AsyncClient, spawns: _Spawns
) -> None:
    await _running_row()
    await store.mark_done(TEAM, CERTIFICATE, PRIVATE_KEY)

    answer = await client.post(PATH, json=QUESTION)

    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    assert answer.json() == {
        "domain": f"{TEAM}.{ZONE}",
        "certificate": CERTIFICATE,
        "private_key": PRIVATE_KEY,
        "sso": {"discovery_url": DISCOVERY, "client_id": "zad-fieldlab", "client_secret": CLIENT_SECRET},
    }
    assert spawns.started == []


async def test_a_failed_attempt_is_reported_once_and_then_starts_over(
    orm_db: Any, client: AsyncClient, spawns: _Spawns
) -> None:
    await _running_row()
    await store.mark_failed(TEAM, Step.CERTIFICATE)

    failed = await client.post(PATH, json=QUESTION)
    again = await client.post(PATH, json=QUESTION)

    assert failed.status_code == 503
    assert "Let's Encrypt" in failed.json()["detail"]
    assert again.status_code == 202
    assert spawns.started == [(TEAM, IP)]


async def test_an_attempt_that_went_quiet_counts_as_failed(orm_db: Any, client: AsyncClient, spawns: _Spawns) -> None:
    """The pod restarted mid-attempt: nobody is going to finish this row."""
    await _running_row()
    await _age_the_row(minutes=11)

    answer = await client.post(PATH, json=QUESTION)

    assert answer.status_code == 503
    assert spawns.started == []
    row = await store.read(TEAM)
    assert row is not None
    assert (row.state, row.step) == (State.FAILED, None)


@pytest.mark.parametrize("code", ["niet-de-code", "code-met-een-accent-\u00e9"])
async def test_a_wrong_code_is_refused_without_a_row(
    orm_db: Any, client: AsyncClient, spawns: _Spawns, code: str
) -> None:
    """A non-ASCII code too: that one is this 401 and not a 500 that skips the limit."""
    answer = await client.post(PATH, json={**QUESTION, "code": code})

    assert answer.status_code == 401
    assert answer.json() == {"detail": "Ongeldige code"}
    assert await store.read(TEAM) is None


async def test_an_empty_code_is_refused(orm_db: Any, client: AsyncClient, spawns: _Spawns) -> None:
    answer = await client.post(PATH, json={**QUESTION, "code": ""})

    assert answer.status_code == 401


async def test_the_failure_limit_refuses_a_guesser(orm_db: Any, client: AsyncClient, spawns: _Spawns) -> None:
    """A wrong code counts down the same limit as a wrong API key."""
    statuses = [
        (await client.post(PATH, json={**QUESTION, "code": f"poging-{attempt}"})).status_code
        for attempt in range(settings.AUTH_FAILURE_LIMIT_PER_MINUTE + 2)
    ]

    assert statuses[0] == 401
    assert statuses[-1] == 429


@pytest.mark.parametrize("team", ["", "Team42", "team_42", "-team", "team-", "x" * 64, "team42.zadlab.nl", "team42\n"])
async def test_a_team_name_that_is_no_dns_label_is_refused(
    orm_db: Any, client: AsyncClient, spawns: _Spawns, team: str
) -> None:
    answer = await client.post(PATH, json={**QUESTION, "team": team})

    assert answer.status_code == 400
    assert "team" in answer.json()["detail"]


@pytest.mark.parametrize("cluster_id", ["", "met een spatie", "x" * 129, "cluster/1", "cluster-1\n"])
async def test_a_cluster_id_outside_its_rule_is_refused(
    orm_db: Any, client: AsyncClient, spawns: _Spawns, cluster_id: str
) -> None:
    answer = await client.post(PATH, json={**QUESTION, "cluster_id": cluster_id})

    assert answer.status_code == 400
    assert "cluster_id" in answer.json()["detail"]


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "::1",
        "geen-adres",
        "",
    ],
)
async def test_an_address_that_is_not_public_ipv4_is_refused(
    orm_db: Any, client: AsyncClient, spawns: _Spawns, ip: str
) -> None:
    answer = await client.post(PATH, json={**QUESTION, "ip": ip})

    assert answer.status_code == 400
    assert "ip" in answer.json()["detail"]


async def test_a_public_address_is_accepted(orm_db: Any, client: AsyncClient, spawns: _Spawns) -> None:
    answer = await client.post(PATH, json={**QUESTION, "ip": "8.8.8.8"})

    assert answer.status_code == 202


async def test_an_unknown_field_is_ignored(orm_db: Any, client: AsyncClient, spawns: _Spawns) -> None:
    """So a field can be added later without an older installer breaking."""
    answer = await client.post(PATH, json={**QUESTION, "toekomstig_veld": "later"})

    assert answer.status_code == 202


async def test_without_the_code_the_route_does_not_exist() -> None:
    with patch.object(settings, "FUNDAMENT_REGISTRATION_CODE", None):
        app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bare:
        answer = await bare.post(PATH, json=QUESTION)

    assert answer.status_code == 404


async def test_a_missing_setting_is_a_503_that_names_it() -> None:
    with ExitStack() as stack:
        _applied(stack, FUNDAMENT_REGISTRATION_SSO_CLIENT_SECRET=None)
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as half_configured:
            answer = await half_configured.post(PATH, json=QUESTION)

    assert answer.status_code == 503
    assert answer.json()["detail"] == "Instellingen ontbreken: FUNDAMENT_REGISTRATION_SSO_CLIENT_SECRET"


async def test_no_request_logs_the_code(
    orm_db: Any, client: AsyncClient, spawns: _Spawns, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither a question that is let through nor one that is refused names the code."""
    with caplog.at_level(logging.DEBUG):
        await client.post(PATH, json=QUESTION)
        await client.post(PATH, json={**QUESTION, "code": "niet-de-code"})

    assert TEAM in caplog.text
    assert CODE not in caplog.text
    assert "niet-de-code" not in caplog.text


async def _answer_with(**secrets: Any) -> Any:
    """The 200 of a finished registration, answered with these extra settings in place."""
    with ExitStack() as stack:
        _applied(stack, **secrets)
        await _running_row()
        await store.mark_done(TEAM, CERTIFICATE, PRIVATE_KEY)
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as caller:
            return await caller.post(PATH, json=QUESTION)


async def test_the_answer_carries_vlam_when_both_secrets_are_set(orm_db: Any, spawns: _Spawns) -> None:
    answer = await _answer_with(**VLAM_SECRETS)

    assert answer.status_code == 200
    assert answer.json()["vlam"] == {
        "login_server": "https://vonk.rijksapp.dev",
        "gateway": "100.80.0.1:443",
        "api_host": "vlam-api.overheid-i.nl",
        "preauth_key": PREAUTH_KEY,
        "api_key": VLAM_API_KEY,
    }


@pytest.mark.parametrize(
    "secrets",
    [
        {},
        {"FUNDAMENT_REGISTRATION_VLAM_PREAUTH_KEY": PREAUTH_KEY},
        {"FUNDAMENT_REGISTRATION_VLAM_API_KEY": VLAM_API_KEY},
    ],
    ids=["neither", "only-preauth", "only-api"],
)
async def test_without_both_secrets_the_field_is_absent(orm_db: Any, spawns: _Spawns, secrets: dict[str, Any]) -> None:
    """Absent, not null and not empty: the rest of the answer has to keep working."""
    answer = await _answer_with(**secrets)

    assert answer.status_code == 200
    assert "vlam" not in answer.json()
    assert answer.json()["domain"] == f"{TEAM}.{ZONE}"


async def test_the_vlam_values_are_not_stored_in_the_table(orm_db: Any, spawns: _Spawns) -> None:
    """They come fresh from the settings, so a replaced key travels with the next answer."""
    await _answer_with(**VLAM_SECRETS)

    async with session_scope() as session:
        row = await session.get(FundamentRegistration, TEAM)

    assert row is not None
    stored = str(row.__dict__)
    assert PREAUTH_KEY not in stored
    assert VLAM_API_KEY not in stored


async def test_no_vlam_secret_reaches_the_log(orm_db: Any, spawns: _Spawns, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        answer = await _answer_with(**VLAM_SECRETS)

    assert answer.status_code == 200
    assert PREAUTH_KEY not in caplog.text
    assert VLAM_API_KEY not in caplog.text
