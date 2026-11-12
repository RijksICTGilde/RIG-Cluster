"""Repeat a call while the other side cannot be reached.

Here and not in a connector, because several hold it: ArgoCD rides out the restart of its
own API server with it (`docs/post-mortems/argocd-server-herstart-elke-40-minuten.md`) and
the mail relay a pod swap, and in both cases that difference otherwise costs a whole
deployment.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

_FIRST_DELAY = 1.0

#: Ceiling on one pause. A caller whose budget runs into minutes still wants to see the
#: recovery within a minute of it happening.
_MAX_DELAY = 60.0


def backoff_delays(first: float, cap: float) -> Iterator[float]:
    """Doubling pauses from ``first``, capped at ``cap`` and then repeating it forever.

    Endless on purpose: the recovery tasks that use it directly must never give up,
    because a pod that stops probing stays broken until someone restarts it.
    """
    delay = first
    while True:
        yield min(delay, cap)
        delay *= 2


async def retry_on_connect_error[T](
    operation: Callable[[], Awaitable[T]],
    budget_seconds: float,
    description: str,
    retry_on: tuple[type[BaseException], ...] | Callable[[BaseException], bool] = (aiohttp.ClientConnectorError,),
) -> T:
    """Repeat an operation while ``retry_on`` accepts its failure, sleeping at most
    budget_seconds in total.

    As a tuple of exception types, ``retry_on`` defaults to ClientConnectorError alone: the
    TCP connection was never established, so the request never reached the server.
    Repeating it is therefore safe for any method, POST included. Read timeouts and
    ServerDisconnectedError are not retried, because there the request may already have
    been processed. Widen it for a caller on another protocol: a PostgreSQL connect fails
    with OSError (ConnectionRefusedError and gaierror are both one), never with an aiohttp
    error. Only name exceptions that mean the connection was never made; an error on a
    query belongs to the caller.

    As a predicate, ``retry_on`` decides per exception, for a caller whose exception type
    carries both a transient and a permanent failure and whose message is all there is to
    tell them apart. Every ``Exception`` then reaches the predicate, and one it rejects is
    raised untouched, so the predicate owns the whole decision.

    A budget of 0 gives exactly one attempt without sleeping. Once the budget is spent the
    last error is raised untouched.
    """
    accepts = retry_on if callable(retry_on) else None
    caught: tuple[type[BaseException], ...] = (Exception,) if accepts else retry_on
    slept = 0.0
    delays = backoff_delays(_FIRST_DELAY, _MAX_DELAY)
    while True:
        try:
            return await operation()
        except caught as e:
            if accepts is not None and not accepts(e):
                raise
            remaining = budget_seconds - slept
            if remaining <= 0:
                logger.error(f"Unreachable after {slept:.0f}s of retries, giving up on {description}: {e}")
                raise
            # Capped at what is left, so budget_seconds really is the upper bound the
            # docstring above promises. The backoff on a budget of 30 is therefore
            # 1, 2, 4, 8, 15.
            pause = min(next(delays), remaining)
            logger.warning(f"Connect failed for {description}, retrying in {pause:.0f}s: {e}")
            await asyncio.sleep(pause)
            slept += pause
