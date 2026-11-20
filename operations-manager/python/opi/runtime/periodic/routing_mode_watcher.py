"""Follows the routing stand of the cluster and brings OPI to it.

The ConfigMap ``zad-routing`` in the namespace of OPI says which entrypoint this cluster has;
the routing component of that stand brings it along, so a switch in git reaches OPI without a
new image. This watcher reads it, waits until the entrypoint of the new stand actually
accepts routes, and only then starts rendering in it and queues a reprocess for every
project. Without that order a project someone edits halfway through the switch would land on
an entrypoint that is not there yet.

The reprocess happens once per switch, across restarts: the adopted stand is in the table
``routing_mode``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from opi.connectors.kubectl import create_kubectl_connector
from opi.core.actor import scheduler_actor, set_actor
from opi.core.cluster_config import (
    RoutingApi,
    get_argo_namespace,
    get_configured_routing_api,
    get_routing_api,
    set_rendered_routing_api,
)
from opi.core.config import settings
from opi.services.project_reprocess import queue_reprocess_of_every_project
from opi.services.project_store import get_project_store
from opi.services.routing_mode_store import RoutingModeStore
from opi.services.routing_readiness import (
    RoutingEntrypointMissingError,
    RoutingEntrypointUnknownError,
    verify_routing_entrypoint,
)

if TYPE_CHECKING:
    from opi.services.tasks.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)

#: Name and key of the ConfigMap the routing component of each stand brings along.
ROUTING_CONFIGMAP = "zad-routing"
ROUTING_CONFIGMAP_KEY = "routing"


class RoutingModeUnknownError(Exception):
    """The ConfigMap could not be read, so nothing is known about the wanted stand."""


async def read_wanted_routing_api(namespace: str) -> RoutingApi | None:
    """The stand the cluster asks for, or ``None`` when there is definitively no ConfigMap.

    ``None`` only for a definitive absence: a cluster without the ConfigMap keeps the stand
    of ``CLUSTER_CONFIG``, and a read that failed must never read as that.

    Raises:
        RoutingModeUnknownError: the ConfigMap could not be read, or carries no usable value.
    """
    kubectl = create_kubectl_connector()
    exists = await kubectl.resource_exists("configmap", ROUTING_CONFIGMAP, namespace)
    if exists is None:
        raise RoutingModeUnknownError(f"Kon niet vaststellen of ConfigMap '{namespace}/{ROUTING_CONFIGMAP}' bestaat.")
    if not exists:
        return None

    configmap = await kubectl.get_namespaced_resource("configmap", ROUTING_CONFIGMAP, namespace)
    if configmap is None:
        raise RoutingModeUnknownError(f"Kon ConfigMap '{namespace}/{ROUTING_CONFIGMAP}' niet lezen.")
    value = (configmap.get("data") or {}).get(ROUTING_CONFIGMAP_KEY)
    try:
        return RoutingApi(value)
    except ValueError as error:
        raise RoutingModeUnknownError(
            f"ConfigMap '{namespace}/{ROUTING_CONFIGMAP}' heeft sleutel '{ROUTING_CONFIGMAP_KEY}' "
            f"met waarde '{value}', en dat is geen bekende stand."
        ) from error


class RoutingModeWatcher:
    """Polls the ConfigMap and performs the switch when the new entrypoint is ready."""

    def __init__(self, task_service: AsyncTaskService, cluster: str, store: RoutingModeStore | None = None) -> None:
        self._task_service = task_service
        self._cluster = cluster
        self._store = store or RoutingModeStore()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Routing mode watcher started (cluster=%s, every %ds)",
            self._cluster,
            settings.ROUTING_MODE_CHECK_INTERVAL_SECONDS,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Routing mode watcher stopped")

    async def _run(self) -> None:
        set_actor(scheduler_actor("routing-mode"))
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in the routing mode check")
            try:
                await asyncio.sleep(settings.ROUTING_MODE_CHECK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

    async def tick(self) -> None:
        """One read of the ConfigMap, and the switch that may follow from it."""
        try:
            wanted = await read_wanted_routing_api(get_argo_namespace(self._cluster))
        except RoutingModeUnknownError as unknown:
            logger.warning(f"De gewenste routing-stand is niet gelezen, de huidige blijft gelden: {unknown}")
            return
        if wanted is None:
            return

        # Against the stand the switch was last COMPLETED for, not against the stand OPI
        # renders in: a switch that finished while OPI was down still owes a reprocess, and
        # no row means no switch has ever been completed, so the configured stand holds.
        adopted = await self._store.adopted(self._cluster)
        if adopted is None:
            adopted = get_configured_routing_api(self._cluster)
        if wanted is adopted:
            if get_routing_api(self._cluster) is not wanted:
                set_rendered_routing_api(self._cluster, wanted)
                logger.info(f"OPI rendert weer in routing-stand '{wanted}' (al eerder overgenomen)")
            return

        try:
            entrypoint = await verify_routing_entrypoint(self._cluster, wanted)
        except RoutingEntrypointMissingError as missing:
            logger.info(f"Routing-stand '{wanted}' nog niet overgenomen: {missing}")
            return
        except RoutingEntrypointUnknownError as unknown:
            logger.warning(f"De ingang van routing-stand '{wanted}' is niet gemeten: {unknown}")
            return

        if get_project_store().cache_head() is None:
            logger.warning(
                f"Routing-stand '{wanted}' nog niet overgenomen: de projectcache is niet geladen, dus een "
                "herverwerking zou nul projecten in de rij zetten."
            )
            return

        set_rendered_routing_api(self._cluster, wanted)
        logger.info(f"OPI rendert vanaf nu in routing-stand '{wanted}' ({entrypoint})")
        task_ids = await queue_reprocess_of_every_project(self._task_service, self._cluster)
        # Recorded AFTER queueing: a crash halfway queues again on the next boot, and a
        # project refreshed twice is harmless where one left on the old API is not.
        await self._store.record(self._cluster, wanted)
        logger.info(f"Routing-wissel naar '{wanted}': {len(task_ids)} project(en) in de rij voor herverwerking")
