"""The backoff arithmetic of ``retry_on_connect_error``.

The budget is the upper bound on time spent asleep, so the last pause is cut to what is
left of it instead of overshooting. Callers pick their budget from how long the outage
they ride out lasts (an argocd-server restart, a pod swap), which only holds if the
promise is kept exactly.
"""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from opi.core.connect_retry import backoff_delays, retry_on_connect_error


def _connect_error() -> aiohttp.ClientConnectorError:
    key = ConnectionKey("host", 80, False, True, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError("connection refused"))


async def _collect_delays(budget: float) -> tuple[list[float], BaseException | None]:
    """Run a never-succeeding operation and report the pauses it slept."""
    delays: list[float] = []

    async def always_fails() -> None:
        raise _connect_error()

    async def record(seconds: float) -> None:
        delays.append(seconds)

    with patch("asyncio.sleep", new=AsyncMock(side_effect=record)):
        try:
            await retry_on_connect_error(always_fails, budget, "test")
        except BaseException as e:  # noqa: BLE001  # the raised error is what is under test
            return delays, e
    return delays, None


async def test_the_pauses_double_and_the_last_one_is_cut_to_the_budget():
    delays, error = await _collect_delays(30.0)

    assert delays == [1.0, 2.0, 4.0, 8.0, 15.0]
    assert sum(delays) == 30.0
    assert isinstance(error, aiohttp.ClientConnectorError), "the original error, not a wrapper"


async def test_a_budget_of_zero_gives_one_attempt_without_sleeping():
    delays, error = await _collect_delays(0.0)

    assert delays == []
    assert isinstance(error, aiohttp.ClientConnectorError)


async def test_a_budget_smaller_than_the_first_pause_still_sleeps_only_that_much():
    delays, _ = await _collect_delays(0.5)

    assert delays == [0.5]


async def test_a_success_after_a_connect_failure_returns_that_result():
    attempts = 0

    async def fails_once() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _connect_error()
        return "ok"

    with patch("asyncio.sleep", new=AsyncMock()):
        assert await retry_on_connect_error(fails_once, 30.0, "test") == "ok"
    assert attempts == 2


async def test_an_error_outside_retry_on_is_not_retried():
    attempts = 0

    async def wrong_error() -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("not a connect failure")

    with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(ValueError, match="not a connect failure"):
        await retry_on_connect_error(wrong_error, 30.0, "test")
    assert attempts == 1


async def test_the_pauses_stop_doubling_at_a_minute():
    delays: list[float] = []

    async def always_fails() -> None:
        raise _connect_error()

    async def record(seconds: float) -> None:
        delays.append(seconds)

    with patch("asyncio.sleep", new=AsyncMock(side_effect=record)), pytest.raises(aiohttp.ClientConnectorError):
        await retry_on_connect_error(always_fails, 400.0, "test")

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0, 60.0, 37.0]
    assert sum(delays) == 400.0


async def test_an_error_the_predicate_rejects_is_raised_at_once():
    attempts = 0

    async def permanent() -> None:
        nonlocal attempts
        attempts += 1
        raise _connect_error()

    with (
        patch("asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(aiohttp.ClientConnectorError),
    ):
        await retry_on_connect_error(permanent, 30.0, "test", retry_on=lambda _: False)

    assert attempts == 1
    sleep.assert_not_awaited()


async def test_an_error_the_predicate_accepts_is_retried():
    attempts = 0

    async def transient_then_ok() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _connect_error()
        return "ok"

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await retry_on_connect_error(
            transient_then_ok, 30.0, "test", retry_on=lambda e: "Cannot connect" in str(e)
        )

    assert result == "ok"
    assert attempts == 2


def test_backoff_delays_doubles_up_to_the_cap_and_then_repeats_it():
    delays = backoff_delays(5.0, 60.0)

    assert [next(delays) for _ in range(8)] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0]


def test_backoff_delays_with_an_equal_first_and_cap_is_a_fixed_pause():
    delays = backoff_delays(30.0, 30.0)

    assert [next(delays) for _ in range(4)] == [30.0, 30.0, 30.0, 30.0]
