"""Tests voor het memoiseren van een geslaagde infrastructuur-credentialtoets (RC-418).

``test_infrastructure_database_connection`` deed voor elke deployment in scope een
echte verbinding om dezelfde superuser-credentials te toetsen. Een geslaagde toets op
precies (host, gebruiker, wachtwoord) blijft gelden zolang het proces leeft: het secret
wijzigt niet binnen een taak, dus een tweede verbinding zou hetzelfde meten.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.manager.project import infrastructure as infrastructure_module
from opi.manager.project.infrastructure import Infrastructure


@pytest.fixture(autouse=True)
def _schone_cache():
    infrastructure_module._validated_infrastructure_credentials.clear()
    yield
    infrastructure_module._validated_infrastructure_credentials.clear()


@pytest.mark.asyncio
async def test_een_geslaagde_toets_wordt_onthouden() -> None:
    connect = AsyncMock(return_value=AsyncMock(close=AsyncMock()))
    with patch("asyncpg.connect", connect):
        eerste = await Infrastructure.test_infrastructure_database_connection("postgres", "wachtwoord", "db-host")
        tweede = await Infrastructure.test_infrastructure_database_connection("postgres", "wachtwoord", "db-host")

    assert eerste == (True, "success")
    assert tweede == (True, "success")
    assert connect.await_count == 1, "de tweede toets had geen nieuwe verbinding mogen opzetten"


@pytest.mark.asyncio
async def test_andere_credentials_worden_wel_echt_getoetst() -> None:
    connect = AsyncMock(return_value=AsyncMock(close=AsyncMock()))
    with patch("asyncpg.connect", connect):
        await Infrastructure.test_infrastructure_database_connection("postgres", "wachtwoord-1", "db-host")
        await Infrastructure.test_infrastructure_database_connection("postgres", "wachtwoord-2", "db-host")

    assert connect.await_count == 2


@pytest.mark.asyncio
async def test_een_mislukte_toets_wordt_niet_onthouden() -> None:
    """Zonder dit zou een echt verkeerd wachtwoord na de eerste fout als geldig gelden."""
    import asyncpg

    connect = AsyncMock(side_effect=asyncpg.InvalidPasswordError("password authentication failed"))
    with patch("asyncpg.connect", connect):
        eerste = await Infrastructure.test_infrastructure_database_connection("postgres", "verkeerd", "db-host")
        tweede = await Infrastructure.test_infrastructure_database_connection("postgres", "verkeerd", "db-host")

    assert eerste == (False, "auth_error")
    assert tweede == (False, "auth_error")
    assert connect.await_count == 2, "een mislukte toets mag niet gememoiseerd worden"
