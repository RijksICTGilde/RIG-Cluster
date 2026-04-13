"""
Chisel tunnel helper utilities.

Provides an async context manager for Chisel tunnel setup/teardown with
automatic password decryption support.
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from opi.connectors.chisel_connector import ChiselConnector
from opi.utils.age import decrypt_password_smart, get_decoded_project_private_key

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@asynccontextmanager
async def chisel_tunnel(
    chisel_config: dict[str, Any],
    remote_host: str,
    remote_port: int,
    project_data: dict[str, Any] | None = None,
) -> AsyncGenerator[dict[str, Any]]:
    """
    Async context manager for Chisel tunnel setup/teardown.

    Handles password decryption and tunnel lifecycle management.

    Args:
        chisel_config: Chisel configuration dict with keys:
            - server-url: Chisel server URL (e.g., "https://chisel.example.com")
            - username: Authentication username
            - password: Authentication password (may be encrypted)
        remote_host: Remote service hostname
        remote_port: Remote service port
        project_data: Project configuration data containing the AGE private key for decryption

    Yields:
        Dict with local endpoint information:
            - host: Local hostname (always "localhost")
            - port: Local port number for the tunnel

    Example:
        async with chisel_tunnel(chisel_config, "postgres.svc", 5432, project_data) as endpoint:
            source_host = endpoint["host"]
            source_port = endpoint["port"]
            # Connect to localhost:{port} to reach remote service
    """
    # Get project's AGE private key for decryption
    private_key = None
    if project_data:
        private_key = await get_decoded_project_private_key(project_data)

    chisel_password = await decrypt_password_smart(
        chisel_config.get("password", ""),
        private_key,
    )

    connector = ChiselConnector(
        server_url=chisel_config["server-url"],
        username=chisel_config["username"],
        password=chisel_password,
    )

    try:
        connector.start_tunnel(remote_host=remote_host, remote_port=remote_port)
        endpoint = connector.get_local_endpoint()
        yield endpoint
    finally:
        connector.stop_tunnel()
