"""An async context manager around a Chisel tunnel, including the password decryption."""

import asyncio
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
    """Set up a Chisel tunnel and tear it down again, yielding the local endpoint.

    ``chisel_config`` carries ``server-url``, ``username`` and ``password``, the last one
    possibly AGE-encrypted; ``project_data`` supplies the private key that opens it. The
    yielded endpoint is ``host`` (always localhost) and ``port``.
    """
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

    # In a thread: start_tunnel polls up to 30s on a synchronous socket connect and
    # stop_tunnel waits for the process to exit.
    try:
        await asyncio.to_thread(connector.start_tunnel, remote_host=remote_host, remote_port=remote_port)
        endpoint = connector.get_local_endpoint()
        yield endpoint
    finally:
        await asyncio.to_thread(connector.stop_tunnel)
