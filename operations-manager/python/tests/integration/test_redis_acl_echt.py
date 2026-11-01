"""De commandoregel van een projectuser tegen een ECHTE Redis (RC-409).

De unit test houdt de argumenten vast, maar niet wat Redis ermee doet: of ``-@admin
-@dangerous`` werkelijk ACL LIST, FLUSHALL en KEYS afsluit en de datacommando's op het
eigen prefix overlaat, zegt alleen Redis zelf. Draai hem tegen een Redis 7.x met het
adminwachtwoord in de omgeving, bijvoorbeeld een port-forward naar de sandbox:

    kubectl -n rig-system port-forward svc/rig-redis 6380:6379
    REDIS_TEST_PORT=6380 REDIS_TEST_PASSWORD=... uv run pytest \
        tests/integration/test_redis_acl_echt.py -m requires_infra

Zonder die variabelen slaat hij zichzelf over, zoals de andere integratietoetsen dat doen
zonder cluster.
"""

from __future__ import annotations

import asyncio
import os
import secrets

import pytest
from opi.manager.redis_manager import RedisManager

pytestmark = pytest.mark.requires_infra

PROBE_USER = "rc409-probe"
PROBE_PREFIX = "rc409-probe"
# Per run verzonnen: de probe-user bestaat alleen binnen deze test, en zo staat er geen
# wachtwoordvorm in de repo die een secretscan als credential moet triëren.
PROBE_PASSWORD = secrets.token_urlsafe(24)


def _admin_password() -> str:
    password = os.environ.get("REDIS_TEST_PASSWORD")
    if not password:
        pytest.skip("REDIS_TEST_PASSWORD niet gezet, geen echte Redis om tegen te meten")
    return password


async def _als_probe(*commands: tuple[str, ...]) -> list[str]:
    """Voer elk commando ALS de probe-user uit en geef de eerste antwoordregel terug.

    Een verbinding per commando: ``_send_redis_command`` leest een regel, en een
    antwoord met een waarde (GET) is er twee, dus op een gedeelde verbinding zou het
    volgende antwoord een regel achterlopen.
    """
    host = os.environ.get("REDIS_TEST_HOST", "127.0.0.1")
    port = int(os.environ.get("REDIS_TEST_PORT", "6379"))
    antwoorden: list[str] = []
    for command in commands:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)
        try:
            auth = await RedisManager._send_redis_command(reader, writer, "AUTH", PROBE_USER, PROBE_PASSWORD)
            assert auth.startswith("+OK"), f"de probe-user kon niet inloggen: {auth}"
            antwoorden.append(await RedisManager._send_redis_command(reader, writer, *command))
        finally:
            writer.close()
            await writer.wait_closed()
    return antwoorden


@pytest.fixture
async def probe_user() -> str:
    """Maak de projectuser aan zoals de manager dat doet, en haal hem daarna weg."""
    host = os.environ.get("REDIS_TEST_HOST", "127.0.0.1")
    port = int(os.environ.get("REDIS_TEST_PORT", "6379"))
    admin = _admin_password()
    await RedisManager._create_acl_user(host, port, admin, PROBE_USER, PROBE_PASSWORD, PROBE_PREFIX)
    try:
        yield PROBE_USER
    finally:
        await RedisManager._delete_acl_user(host, port, admin, PROBE_USER)


async def test_beheer_en_flush_zijn_dicht(probe_user: str) -> None:
    antwoorden = await _als_probe(("ACL", "LIST"), ("FLUSHALL",), ("KEYS", "*"), ("CONFIG", "GET", "maxmemory"))

    for commando, antwoord in zip(("ACL LIST", "FLUSHALL", "KEYS", "CONFIG GET"), antwoorden, strict=True):
        assert "NOPERM" in antwoord, f"{commando} mag nog: {antwoord}"


async def test_de_eigen_sleutels_blijven_werken(probe_user: str) -> None:
    gezet, gelezen, vreemd = await _als_probe(
        ("SET", f"{PROBE_PREFIX}:x", "1"),
        ("GET", f"{PROBE_PREFIX}:x"),
        ("GET", "ander-project:x"),
    )

    assert gezet.startswith("+OK"), gezet
    assert gelezen.startswith("$1"), gelezen
    assert "NOPERM" in vreemd, f"een sleutel buiten het prefix mag nog: {vreemd}"
