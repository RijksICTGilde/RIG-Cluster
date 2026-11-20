"""The routing stand this cluster has been brought to, in the table ``routing_mode``.

Read and written by ``RoutingModeWatcher``, so that one switch reprocesses every project
exactly once, also across a restart of OPI.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from opi.core.cluster_config import RoutingApi
from opi.core.db import session_scope
from opi.services.persistence.routing_mode import RoutingMode

logger = logging.getLogger(__name__)


class RoutingModeStore:
    async def adopted(self, cluster: str) -> RoutingApi | None:
        """The stand this cluster was last brought to, or ``None`` when there is no row."""
        async with session_scope() as session:
            row = await session.get(RoutingMode, cluster)
        if row is None:
            return None
        try:
            return RoutingApi(row.routing)
        except ValueError:
            logger.warning(f"routing_mode of cluster '{cluster}' carries unknown stand '{row.routing}'")
            return None

    async def record(self, cluster: str, routing: RoutingApi) -> None:
        async with session_scope() as session:
            await session.merge(RoutingMode(cluster=cluster, routing=routing.value, adopted_at=datetime.now(UTC)))
