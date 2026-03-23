"""Graceful shutdown coordination.

When a shutdown signal (SIGTERM) is received, the application enters "drain mode":
- The task worker stops claiming new tasks from the queue
- API endpoints that create new tasks return 503
- The UI and all read endpoints keep working normally
- In-flight tasks run to completion before the process exits
"""

import logging

logger = logging.getLogger(__name__)

_draining = False


def begin_drain() -> None:
    """Enter drain mode. Called on SIGTERM before lifespan teardown."""
    global _draining
    if not _draining:
        _draining = True
        logger.info("Entering drain mode: no new tasks will be accepted or claimed")


def is_draining() -> bool:
    """True when the application is shutting down gracefully."""
    return _draining
