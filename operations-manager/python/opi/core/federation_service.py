"""Federation service -- thin routing proxy for multi-cluster task delegation.

When the master OPI receives a task targeting a remote cluster, the service
forwards the creation request to the correct slave OPI instance and stores
a lightweight routing entry so subsequent status queries can be proxied back.
"""

import logging
from typing import TYPE_CHECKING

from opi.connectors.opi import OpiConnector, OpiConnectorError
from opi.core.config import settings
from opi.services.project_store import get_project_store

if TYPE_CHECKING:
    from opi.core.federation_config import FederationRegistry, PeerConfig

logger = logging.getLogger(__name__)


class FederationError(Exception):
    """Raised on federation routing failures."""


class FederationService:
    """Routes async tasks to the correct OPI instance (local or remote)."""

    def __init__(self, registry: FederationRegistry, task_service: object) -> None:
        self._registry = registry
        self._task_service = task_service
        # In-memory route table: {task_id -> peer_url}
        self._route_table: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_task(
        self,
        task_type: str,
        project_name: str,
        deployment_name: str | None,
        target_cluster: str,
        payload: dict,
        created_by: str | None = None,
    ) -> dict:
        """Create a task on the appropriate cluster.

        If *target_cluster* is the local cluster, delegates directly to the
        local task_service. Otherwise forwards the request to the slave OPI
        instance and stores the routing entry.
        """
        if self._registry.is_local_cluster(target_cluster):
            logger.info(
                "Federation: local task %s for %s/%s",
                task_type,
                project_name,
                deployment_name,
            )
            return await self._task_service.create_task(
                task_type=task_type,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=target_cluster,
                payload=payload,
                created_by=created_by,
            )

        peer = self._registry.get_peer(target_cluster)
        if peer is None:
            msg = f"Unknown target cluster: {target_cluster}"
            raise FederationError(msg)

        logger.info(
            "Federation: forwarding %s for %s/%s to cluster %s (%s)",
            task_type,
            project_name,
            deployment_name,
            target_cluster,
            peer.url,
        )

        try:
            async with OpiConnector(
                base_url=peer.url,
                api_key=peer.api_key,
                verify_tls=peer.verify_tls,
                timeout=peer.timeout,
            ) as conn:
                result = await conn.create_task(
                    task_type=task_type,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=target_cluster,
                    payload=payload,
                    created_by=created_by,
                )
        except OpiConnectorError as exc:
            msg = f"Slave {target_cluster} rejected task: {exc}"
            raise FederationError(msg) from exc

        task_id = result.get("task_id", "")
        if task_id:
            self._route_table[task_id] = peer.url
            logger.info("Federation: routed task %s -> %s", task_id, peer.url)

        return result

    async def get_task_status(self, task_id: str) -> dict | None:
        """Get task status -- local DB first, then proxy via route table."""
        # Check local first
        local = await self._task_service.get_task(task_id)
        if local is not None:
            return local

        # Try route table
        peer_url = self._route_table.get(task_id)
        if peer_url is None:
            return None

        peer = self._find_peer_by_url(peer_url)
        if peer is None:
            logger.warning("Federation: route table points to unknown peer url %s", peer_url)
            return None

        try:
            async with OpiConnector(
                base_url=peer.url,
                api_key=peer.api_key,
                verify_tls=peer.verify_tls,
                timeout=peer.timeout,
            ) as conn:
                return await conn.get_task_status(task_id)
        except OpiConnectorError as exc:
            logger.warning("Federation: failed to proxy status for %s: %s", task_id, exc)
            return None

    async def cancel_task(self, task_id: str) -> dict | None:
        """Cancel a task -- local first, then proxy via route table."""
        local = await self._task_service.get_task(task_id)
        if local is not None:
            if local.get("status") == "pending":
                await self._task_service.update_task_status(task_id, "cancelled")
                return {"status": "cancelled", "task_id": task_id}
            return None

        peer_url = self._route_table.get(task_id)
        if peer_url is None:
            return None

        peer = self._find_peer_by_url(peer_url)
        if peer is None:
            return None

        try:
            async with OpiConnector(
                base_url=peer.url,
                api_key=peer.api_key,
                verify_tls=peer.verify_tls,
                timeout=peer.timeout,
            ) as conn:
                return await conn.cancel_task(task_id)
        except OpiConnectorError as exc:
            logger.warning("Federation: failed to proxy cancel for %s: %s", task_id, exc)
            return None

    async def resolve_cluster(self, project_name: str, deployment_name: str | None) -> str:
        """Determine the target cluster for a deployment.

        Reads the deployment's cluster from the in-memory project cache (the same
        source other hot-path readers use); on a cache miss it falls back to a
        single git read, closing the git connector afterwards so the federation
        routing path does not re-clone the projects repo and leak connectors on
        every call. Falls back to the local CLUSTER_MANAGER setting on any error.
        """
        if not deployment_name:
            return settings.CLUSTER_MANAGER

        try:
            from opi.handlers.project_file_handler import ProjectFileHandler

            handler = ProjectFileHandler()

            # Primary: in-memory cache (no clone).
            cached = get_project_store().get(project_name)
            if cached is not None and cached.data is not None:
                cluster = handler.extract_deployment_cluster(cached.data, deployment_name)
                if cluster:
                    return cluster

            # Fallback: single git read; always close the connector.
            from opi.manager.project_manager import ProjectManager

            project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
            try:
                project_data = await project_manager.get_contents()
            finally:
                await project_manager.close()
            if project_data is None:
                logger.warning(
                    "Federation: no project data for %s, falling back to local cluster",
                    project_name,
                )
                return settings.CLUSTER_MANAGER

            cluster = handler.extract_deployment_cluster(project_data, deployment_name)
            if cluster:
                return cluster
        except Exception as exc:
            logger.warning(
                "Federation: failed to resolve cluster for %s/%s: %s",
                project_name,
                deployment_name,
                exc,
            )

        return settings.CLUSTER_MANAGER

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_peer_by_url(self, url: str) -> PeerConfig | None:
        """Find a peer config whose URL matches the given url."""
        for peer in self._registry.get_all_peers():
            if peer.url == url:
                return peer
        return None
