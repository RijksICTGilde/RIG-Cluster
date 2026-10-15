"""Een aiohttp-aanroep herhalen zolang de verbinding niet tot stand komt.

Hier en niet in een connector, want twee connectors hebben hem: ArgoCD rijdt er de
herstart van zijn eigen API-server mee uit (docs/argocd-server-herstart-elke-40-minuten.md)
en de mailrelay een podwissel, en in beide gevallen kost dat verschil anders een hele
uitrol.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_FIRST_DELAY = 1.0


async def retry_on_connect_error[T](
    operation: Callable[[], Awaitable[T]],
    budget_seconds: float,
    description: str,
) -> T:
    """Repeat an operation while it cannot connect, spending at most budget_seconds asleep.

    Only ClientConnectorError is caught: the TCP connection was never established, so the
    request never reached the server. Repeating it is therefore safe for any method, POST
    included. Read timeouts and ServerDisconnectedError are not retried, because there the
    request may already have been processed.

    A budget of 0 gives exactly one attempt without sleeping. Once the budget is spent the
    last ClientConnectorError is raised untouched, so a caller sees the same failure as
    before this retry existed.
    """
    slept = 0.0
    delay = _FIRST_DELAY
    while True:
        try:
            return await operation()
        except aiohttp.ClientConnectorError as e:
            remaining = budget_seconds - slept
            if remaining <= 0:
                logger.error(f"Unreachable after {slept:.0f}s of retries, giving up on {description}: {e}")
                raise
            # Capped at what is left, so budget_seconds really is the upper bound the
            # docstring above promises. The backoff on a budget of 30 is therefore
            # 1, 2, 4, 8, 15.
            pause = min(delay, remaining)
            logger.warning(f"Connect failed for {description}, retrying in {pause:.0f}s: {e}")
            await asyncio.sleep(pause)
            slept += pause
            delay *= 2
