"""Port forwarding to a remote service over a Chisel tunnel."""

import logging
import socket
import subprocess
import time
from contextlib import contextmanager
from typing import IO, TYPE_CHECKING, Self, TypedDict

from opi.core.subprocess_env import child_env

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

logger = logging.getLogger(__name__)


class TunnelEndpoint(TypedDict):
    """The local side of a chisel tunnel."""

    host: str
    port: int


class ChiselConnector:
    """One tunnel, run as a chisel subprocess that is started on demand."""

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        timeout: int = 30,
    ) -> None:
        """``timeout`` is in seconds and bounds how long ``start_tunnel`` waits for the port."""
        self.server_url = server_url
        self.auth = f"{username}:{password}"
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self.local_port: int | None = None

    def start_tunnel(
        self,
        remote_host: str,
        remote_port: int,
        local_port: int | None = None,
    ) -> int:
        """Open the tunnel and return the local port that forwards to the remote service.

        ``local_port`` is allocated when it is None.

        Raises:
            RuntimeError: when a tunnel is already running, or the port stays closed.
        """
        if self.process:
            raise RuntimeError("Tunnel already running. Stop it first.")

        if local_port is None:
            local_port = self._get_free_port()

        self.local_port = local_port
        tunnel_spec = f"{local_port}:{remote_host}:{remote_port}"

        cmd = [
            "chisel",
            "client",
            self.server_url,
            tunnel_spec,
        ]

        logger.info(
            f"Starting Chisel tunnel: server={self.server_url}, "
            f"local=localhost:{local_port} -> remote={remote_host}:{remote_port}"
        )

        # AUTH rather than --auth: a command line is readable by every process in the
        # container through /proc/<pid>/cmdline.
        self.process = subprocess.Popen(  # noqa: S603  # no shell; the variable parts are separate argv elements
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env({"AUTH": self.auth}),
        )

        if not self._wait_for_port(local_port):
            # Collect before stopping: stop_tunnel drops the process and with it its pipes.
            error_details = self._collect_process_output()
            self.stop_tunnel()
            raise RuntimeError(
                f"Chisel tunnel failed to connect to {self.server_url} within {self.timeout}s. "
                f"Remote target: {remote_host}:{remote_port}. "
                f"Error: {error_details}"
            )

        logger.info(
            f"Chisel tunnel established: localhost:{local_port} -> {remote_host}:{remote_port} (via {self.server_url})"
        )
        return local_port

    def _collect_process_output(self) -> str:
        """Collect stdout/stderr from the chisel process for error reporting."""
        if not self.process:
            return "No process"

        output_lines = []

        # chisel logs to stderr
        if self.process.stderr:
            stderr_content = self._read_available_output(self.process.stderr)
            if stderr_content:
                output_lines.extend(stderr_content)

        if self.process.stdout:
            stdout_content = self._read_available_output(self.process.stdout)
            if stdout_content:
                output_lines.extend(stdout_content)

        return "; ".join(output_lines[-5:]) if output_lines else "No output captured"

    def _read_available_output(self, stream: IO[str]) -> list[str]:
        """Read available output from a stream without blocking."""
        import select

        lines = []
        try:
            while select.select([stream], [], [], 0)[0]:
                line = stream.readline()
                if not line:
                    break
                lines.append(line.strip())
        except (OSError, ValueError) as e:
            logger.debug(f"Error reading chisel output: {e}")
        return lines

    def stop_tunnel(self) -> None:
        """Terminate the tunnel subprocess; a warning and no error when there is none."""
        if not self.process:
            logger.warning("No active tunnel to stop")
            return

        logger.info(f"Stopping Chisel tunnel (localhost:{self.local_port})")
        self.process.terminate()

        try:
            self.process.wait(timeout=5)
            logger.info("✓ Chisel tunnel stopped")
        except subprocess.TimeoutExpired:
            logger.warning("Chisel didn't terminate gracefully, forcing kill")
            self.process.kill()
            self.process.wait()

        self.process = None
        self.local_port = None

    def get_local_endpoint(self) -> TunnelEndpoint:
        """Raises ``RuntimeError`` when no tunnel is running."""
        if not self.process or not self.local_port:
            raise RuntimeError("No active tunnel")

        return {
            "host": "localhost",
            "port": self.local_port,
        }

    @staticmethod
    def _get_free_port() -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _wait_for_port(self, port: int) -> bool:
        """Poll until the port accepts connections; False on timeout or a dead process."""
        start_time = time.time()
        check_count = 0

        while time.time() - start_time < self.timeout:
            check_count += 1

            if self.process.poll() is not None:
                exit_code = self.process.returncode
                stderr = self.process.stderr.read() if self.process.stderr else ""
                logger.error(
                    f"Chisel process exited with code {exit_code} after {check_count} checks. "
                    f"Server: {self.server_url}. stderr: {stderr}"
                )
                return False

            if self._is_port_open("localhost", port):
                logger.debug(f"Chisel tunnel port {port} is now listening (after {check_count} checks)")
                return True

            if check_count % 10 == 0:
                elapsed = time.time() - start_time
                logger.debug(
                    f"Waiting for Chisel tunnel on port {port}... ({elapsed:.1f}s elapsed, {check_count} checks)"
                )

            time.sleep(0.5)

        logger.error(
            f"Chisel tunnel timed out after {self.timeout}s waiting for port {port}. Server: {self.server_url}"
        )
        return False

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            sock.connect((host, port))
            sock.close()
            return True
        except TimeoutError, ConnectionRefusedError, OSError:
            return False

    def __enter__(self) -> Self:
        """Does not open the tunnel: call ``start_tunnel`` yourself."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        self.stop_tunnel()


@contextmanager
def chisel_tunnel(
    server_url: str,
    username: str,
    password: str,
    remote_host: str,
    remote_port: int,
    local_port: int | None = None,
) -> Iterator[ChiselConnector]:
    """Open a tunnel for the duration of the block and stop it afterwards."""
    connector = ChiselConnector(server_url, username, password)
    try:
        connector.start_tunnel(remote_host, remote_port, local_port)
        yield connector
    finally:
        connector.stop_tunnel()
