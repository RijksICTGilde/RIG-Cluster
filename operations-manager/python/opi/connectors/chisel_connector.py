"""
Chisel Tunnel Connector

Provides transparent port forwarding to remote services via Chisel tunnels.
This infrastructure layer handles tunnel lifecycle, authentication, and port management.
"""

import logging
import socket
import subprocess
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class ChiselConnector:
    """
    Manages Chisel tunnel lifecycle.

    Provides transparent port forwarding to remote services through HTTP/HTTPS tunnels.
    The tunnel is managed as a subprocess, allowing on-demand activation.

    Example:
        connector = ChiselConnector(
            server_url="https://chisel.example.com",
            username="admin",
            password="secret"
        )
        local_port = connector.start_tunnel("postgres.svc", 5432)
        # Now connect to localhost:{local_port}
        connector.stop_tunnel()
    """

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        timeout: int = 30,
    ):
        """
        Initialize Chisel connector.

        Args:
            server_url: Chisel server URL (e.g., https://chisel.example.com)
            username: Authentication username
            password: Authentication password
            timeout: Seconds to wait for tunnel to be ready (default: 30)
        """
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
        """
        Start a tunnel to a remote service.

        Creates a local TCP listener that forwards all traffic through the Chisel
        tunnel to the remote service. The tunnel runs as a subprocess.

        Args:
            remote_host: Remote service hostname (e.g., "postgres.namespace.svc.cluster.local")
            remote_port: Remote service port (e.g., 5432)
            local_port: Local port to bind (auto-allocate if None)

        Returns:
            Local port number that forwards to remote service

        Raises:
            RuntimeError: If tunnel is already running or fails to start
        """
        if self.process:
            raise RuntimeError("Tunnel already running. Stop it first.")

        # Auto-allocate port if not specified
        if local_port is None:
            local_port = self._get_free_port()

        self.local_port = local_port
        tunnel_spec = f"{local_port}:{remote_host}:{remote_port}"

        cmd = [
            "chisel",
            "client",
            "--auth",
            self.auth,
            self.server_url,
            tunnel_spec,
        ]

        logger.info(
            f"Starting Chisel tunnel: server={self.server_url}, "
            f"local=localhost:{local_port} -> remote={remote_host}:{remote_port}"
        )

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for tunnel to be ready
        if not self._wait_for_port(local_port):
            # Collect error output before stopping
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

        # Read stderr if available (chisel logs to stderr)
        if self.process.stderr:
            stderr_content = self._read_available_output(self.process.stderr)
            if stderr_content:
                output_lines.extend(stderr_content)

        # Read stdout if available
        if self.process.stdout:
            stdout_content = self._read_available_output(self.process.stdout)
            if stdout_content:
                output_lines.extend(stdout_content)

        return "; ".join(output_lines[-5:]) if output_lines else "No output captured"

    def _read_available_output(self, stream) -> list[str]:
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

    def stop_tunnel(self):
        """
        Stop the active tunnel.

        Terminates the chisel subprocess gracefully. If it doesn't terminate
        within 5 seconds, forces termination.
        """
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

    def get_local_endpoint(self) -> dict[str, str | int]:
        """
        Get the local endpoint for the tunneled service.

        Returns:
            Dict with 'host' and 'port' keys

        Raises:
            RuntimeError: If no active tunnel exists
        """
        if not self.process or not self.local_port:
            raise RuntimeError("No active tunnel")

        return {
            "host": "localhost",
            "port": self.local_port,
        }

    @staticmethod
    def _get_free_port() -> int:
        """
        Find an available local port.

        Returns:
            Available port number
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _wait_for_port(self, port: int) -> bool:
        """
        Wait for port to be listening.

        Polls the port until it accepts connections or timeout is reached.

        Args:
            port: Port number to check

        Returns:
            True if port is listening, False if timeout or process died
        """
        start_time = time.time()
        check_count = 0

        while time.time() - start_time < self.timeout:
            check_count += 1

            # Check if process died
            if self.process.poll() is not None:
                exit_code = self.process.returncode
                stderr = self.process.stderr.read() if self.process.stderr else ""
                logger.error(
                    f"Chisel process exited with code {exit_code} after {check_count} checks. "
                    f"Server: {self.server_url}. stderr: {stderr}"
                )
                return False

            # Check if port is listening
            if self._is_port_open("localhost", port):
                logger.debug(f"Chisel tunnel port {port} is now listening (after {check_count} checks)")
                return True

            # Log progress every 10 checks (5 seconds)
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
        """
        Check if a port is listening.

        Args:
            host: Hostname to check
            port: Port number to check

        Returns:
            True if port accepts connections, False otherwise
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            sock.connect((host, port))
            sock.close()
            return True
        except (TimeoutError, ConnectionRefusedError, OSError):
            return False

    def __enter__(self):
        """Context manager entry - must call start_tunnel() separately."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures tunnel is stopped."""
        self.stop_tunnel()


@contextmanager
def chisel_tunnel(
    server_url: str,
    username: str,
    password: str,
    remote_host: str,
    remote_port: int,
    local_port: int | None = None,
):
    """
    Context manager for easy tunnel usage.

    Automatically starts and stops a Chisel tunnel within a context.

    Args:
        server_url: Chisel server URL (e.g., https://chisel.example.com)
        username: Authentication username
        password: Authentication password
        remote_host: Remote service hostname
        remote_port: Remote service port
        local_port: Local port to bind (auto-allocate if None)

    Yields:
        ChiselConnector instance with active tunnel

    Example:
        with chisel_tunnel(
            server_url="https://chisel.example.com",
            username="admin",
            password="secret",
            remote_host="postgres.namespace.svc",
            remote_port=5432
        ) as tunnel:
            endpoint = tunnel.get_local_endpoint()
            # Connect to endpoint['host']:endpoint['port']
    """
    connector = ChiselConnector(server_url, username, password)
    try:
        connector.start_tunnel(remote_host, remote_port, local_port)
        yield connector
    finally:
        connector.stop_tunnel()
