"""Cloning a database or bucket from an external source: the one path every entry runs.

The v1 and v2 clone routes, their task handlers and the v1 synchronous variant each chose
between a direct connection and a chisel tunnel themselves, and each had its own refusal for
a half-named source. That choice and that refusal live here; the managers underneath do the
copying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from opi.manager.project_manager import ProjectManager
from opi.utils.egress_guard import has_direct_clone_source

logger = logging.getLogger(__name__)

#: What a caller is told when a direct source names only half of its endpoint. One text, so
#: the routes and the task handlers refuse in the same words.
CLONE_SOURCE_INCOMPLETE = "sourceHost and sourcePort are required when not using tunnel configuration"


class CloneSourceIncompleteError(ValueError):
    """A direct clone source without a host or a port: there is nothing to dial."""


@dataclass(frozen=True)
class ChiselTunnel:
    """The chisel server a clone reaches its source through."""

    server_url: str
    username: str
    password: str
    remote_host: str
    remote_port: int


@dataclass(frozen=True)
class ExternalSource:
    """Where a clone reads from: a directly reachable host and port, or a chisel tunnel."""

    host: str | None = None
    port: int | None = None
    tunnel: ChiselTunnel | None = None

    @property
    def is_reachable(self) -> bool:
        """Whether this source can be dialled at all: a tunnel, or both a host and a port."""
        return self.tunnel is not None or has_direct_clone_source(self.host, self.port)

    def direct_endpoint(self) -> tuple[str, int]:
        """The host and port of a direct source.

        Raises:
            CloneSourceIncompleteError: when either of the two is missing.
        """
        if self.host is None or self.port is None:
            raise CloneSourceIncompleteError(CLONE_SOURCE_INCOMPLETE)
        return self.host, self.port


@dataclass(frozen=True)
class DatabaseCloneRequest:
    """One database clone: which deployment reads which database from which source."""

    project_name: str
    deployment_name: str
    source: ExternalSource
    database: str
    schema: str
    username: str
    password: str
    force_clone: bool = False


@dataclass(frozen=True)
class BucketCloneRequest:
    """One bucket clone: which deployment reads which bucket from which source."""

    project_name: str
    deployment_name: str
    source: ExternalSource
    bucket: str
    access_key: str
    secret_key: str
    secure: bool = False
    force_clone: bool = False


async def clone_database_from_external(request: DatabaseCloneRequest) -> dict[str, Any]:
    """Clone a database from an external source into the database of this deployment.

    Raises:
        CloneSourceIncompleteError: when a direct source names no host or no port.
    """
    project_manager = ProjectManager(project_file_relative_path=f"projects/{request.project_name}.yaml")
    try:
        tunnel = request.source.tunnel
        if tunnel:
            logger.info(
                "External database clone via Chisel tunnel: %s/%s <- %s:%s/%s (via %s)",
                request.project_name,
                request.deployment_name,
                tunnel.remote_host,
                tunnel.remote_port,
                request.database,
                tunnel.server_url,
            )
            return await project_manager.clone_database_from_external_with_tunnel(
                project_name=request.project_name,
                deployment_name=request.deployment_name,
                source_database=request.database,
                source_schema=request.schema,
                source_username=request.username,
                source_password=request.password,
                tunnel_server_url=tunnel.server_url,
                tunnel_username=tunnel.username,
                tunnel_password=tunnel.password,
                tunnel_remote_host=tunnel.remote_host,
                tunnel_remote_port=tunnel.remote_port,
                force_clone=request.force_clone,
            )

        host, port = request.source.direct_endpoint()
        logger.info(
            "External database clone (direct): %s/%s <- %s:%s/%s",
            request.project_name,
            request.deployment_name,
            host,
            port,
            request.database,
        )
        return await project_manager.clone_database_from_external_direct(
            project_name=request.project_name,
            deployment_name=request.deployment_name,
            source_host=host,
            source_port=port,
            source_database=request.database,
            source_schema=request.schema,
            source_username=request.username,
            source_password=request.password,
            force_clone=request.force_clone,
        )
    finally:
        await project_manager.close()


async def clone_bucket_from_external(request: BucketCloneRequest) -> dict[str, Any]:
    """Clone a bucket from an external source into the bucket of this deployment.

    Raises:
        CloneSourceIncompleteError: when a direct source names no host or no port.
    """
    project_manager = ProjectManager(project_file_relative_path=f"projects/{request.project_name}.yaml")
    try:
        tunnel = request.source.tunnel
        if tunnel:
            logger.info(
                "External bucket clone via Chisel tunnel: %s/%s <- %s:%s/%s (via %s)",
                request.project_name,
                request.deployment_name,
                tunnel.remote_host,
                tunnel.remote_port,
                request.bucket,
                tunnel.server_url,
            )
            return await project_manager.clone_minio_bucket_from_external_with_tunnel(
                project_name=request.project_name,
                deployment_name=request.deployment_name,
                source_bucket=request.bucket,
                source_access_key=request.access_key,
                source_secret_key=request.secret_key,
                tunnel_server_url=tunnel.server_url,
                tunnel_username=tunnel.username,
                tunnel_password=tunnel.password,
                tunnel_remote_host=tunnel.remote_host,
                tunnel_remote_port=tunnel.remote_port,
                source_secure=request.secure,
                force_clone=request.force_clone,
            )

        host, port = request.source.direct_endpoint()
        logger.info(
            "External bucket clone (direct): %s/%s <- %s:%s/%s",
            request.project_name,
            request.deployment_name,
            host,
            port,
            request.bucket,
        )
        return await project_manager.clone_minio_bucket_from_external_direct(
            project_name=request.project_name,
            deployment_name=request.deployment_name,
            source_host=host,
            source_port=port,
            source_bucket=request.bucket,
            source_access_key=request.access_key,
            source_secret_key=request.secret_key,
            source_secure=request.secure,
            force_clone=request.force_clone,
        )
    finally:
        await project_manager.close()
