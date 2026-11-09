"""How long the database pool keeps trying, and what the caller gets when it gives up.

The startup blocks on this: a database that is briefly away must not fail the boot. The
other half is pinned here too, because it is not obviously wanted: a wrong password is
retried just as long as a refused connection, so a credential mistake costs the same seven
minutes before anything says so.
"""

from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from opi.core import database_pools
from opi.core.database_pool import DatabasePool, DatabasePoolError


async def _initialize_until_it_gives_up(error: BaseException) -> tuple[list[float], BaseException | None]:
    """Let pool initialization keep failing, and report the pauses and the final error."""
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    pool = DatabasePool(host="db", user="u", password="p", database="d", sslmode="disable")
    with (
        patch.object(DatabasePool, "initialize", new=AsyncMock(side_effect=error)),
        patch("asyncio.sleep", new=AsyncMock(side_effect=record)),
    ):
        try:
            await database_pools._initialize_database_pool_with_retry(pool, "main")
        except BaseException as e:  # noqa: BLE001  # the error the caller gets is what is under test
            return delays, e
    return delays, None


async def test_a_refused_connection_is_retried_for_about_seven_minutes():
    delays, error = await _initialize_until_it_gives_up(ConnectionRefusedError("connection refused"))

    assert error is not None
    assert len(delays) >= 10, "a database that is away for minutes must not fail the boot"
    assert 300 <= sum(delays) <= 500, f"about seven minutes of waiting, measured {sum(delays)}s"


async def test_a_wrong_password_is_retried_just_as_long_as_an_outage():
    """Not wanted, only measured: a permanent refusal is indistinguishable from an outage here."""
    delays, error = await _initialize_until_it_gives_up(
        asyncpg.exceptions.InvalidPasswordError("password authentication failed for user")
    )

    assert error is not None
    assert len(delays) >= 10
    assert 300 <= sum(delays) <= 500


async def test_the_caller_gets_the_real_failure_and_not_a_wrapper():
    """The log line and the readiness message are built from this error, so it has to name
    what actually went wrong."""
    _, error = await _initialize_until_it_gives_up(ConnectionRefusedError("connection refused"))

    assert isinstance(error, ConnectionRefusedError)


async def test_initialize_database_pools_turns_that_into_a_pool_error():
    """The message the readiness state shows comes from here, not from the retry."""
    with (
        patch.object(database_pools, "_ensure_database_exists", new=AsyncMock()),
        patch.object(DatabasePool, "initialize", new=AsyncMock(side_effect=ConnectionRefusedError("refused"))),
        patch("asyncio.sleep", new=AsyncMock()),
        pytest.raises(DatabasePoolError),
    ):
        await database_pools.initialize_database_pools()
