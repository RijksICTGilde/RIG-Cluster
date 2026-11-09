"""A listening socket that never answers, for asserting that a call gives up by itself.

The kernel completes the TCP handshake from the listen backlog, so connecting and sending
succeed and only the response never comes: exactly the failure a missing timeout turns
into a hang of minutes.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def unresponsive_server() -> Iterator[str]:
    """Yields the base URL of a server that accepts the connection and sends nothing."""
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        yield f"http://{host}:{port}"
    finally:
        listener.close()
