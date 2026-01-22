"""
WebSocket endpoint for real-time log streaming.

This module provides a WebSocket endpoint that streams deployment logs
in real-time using kubectl logs -f.

Security features:
- Session-based authentication (same as web UI)
- Project-level authorization check
- Connection limits per user and globally
- Rate limiting on log messages
- Log content sanitization

Note on multi-worker deployments:
Connection limits are per-worker. For true global limits across workers,
use Redis or a shared state backend. Current implementation provides
per-worker protection which is sufficient for most deployments.
"""

import asyncio
import contextlib
import html
import json
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.services.project_service import get_project_service
from opi.services.user_service import get_user_service
from opi.utils.naming import generate_unique_name
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

logs_websocket_router: APIRouter = APIRouter(
    prefix="/api/logs",
    tags=["logs-websocket"],
)

# Connection tracking for rate limiting
# Note: This is per-worker state. In multi-worker deployments, limits are per-worker.
_active_connections: dict[str, set[WebSocket]] = defaultdict(set)  # user_email -> set of websockets
_global_connections: set[WebSocket] = set()
_connection_lock = asyncio.Lock()  # Protect concurrent access within a worker

# Limits
MAX_CONNECTIONS_PER_USER = 5
MAX_GLOBAL_CONNECTIONS = 100
MAX_MESSAGES_PER_SECOND = 100  # Rate limit for log messages
SESSION_MAX_AGE_SECONDS = 86400  # 24 hours - sessions older than this are rejected

# Heartbeat interval
HEARTBEAT_INTERVAL_SECONDS = 30


async def send_message(websocket: WebSocket, msg_type: str, **kwargs: Any) -> bool:
    """Send a JSON message over WebSocket. Returns False if connection is closed."""
    try:
        if websocket.client_state == WebSocketState.CONNECTED:
            message = {"type": msg_type, **kwargs}
            await websocket.send_text(json.dumps(message))
            return True
    except Exception as e:
        logger.debug(f"Failed to send WebSocket message: {e}")
    return False


def _get_session_from_cookie(websocket: WebSocket) -> dict[str, Any] | None:
    """
    Extract and validate session data from WebSocket cookies.

    This replicates the session extraction done by Starlette's SessionMiddleware.
    Starlette uses itsdangerous.URLSafeTimedSerializer with the SECRET_KEY.

    Returns:
        Session data dict if valid, None otherwise
    """
    try:
        # Get session cookie from WebSocket headers
        cookies = websocket.cookies
        session_cookie = cookies.get("session")

        if not session_cookie:
            logger.debug("No session cookie found in WebSocket request")
            return None

        # Use the same SECRET_KEY as Starlette's SessionMiddleware
        secret_key = settings.SECRET_KEY
        if not secret_key:
            logger.error("SECRET_KEY not configured")
            return None

        # Starlette's SessionMiddleware uses URLSafeTimedSerializer
        # with "cookie-session" as the salt (default)
        serializer = URLSafeTimedSerializer(secret_key)

        # Validate with max_age to prevent replay of old sessions
        try:
            session_data = serializer.loads(
                session_cookie,
                max_age=SESSION_MAX_AGE_SECONDS,
                salt="cookie-session",  # Starlette's default salt
            )
        except SignatureExpired:
            logger.warning("Session cookie has expired")
            return None
        except BadSignature:
            logger.warning("Session cookie has invalid signature")
            return None

        if isinstance(session_data, dict):
            return session_data

        logger.warning(f"Session data is not a dict: {type(session_data)}")
        return None

    except Exception as e:
        logger.warning(f"Failed to decode session cookie: {e}")
        return None


def _get_user_from_session(session: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract user info from session data."""
    if not session:
        return None
    return session.get("user")


async def _register_connection(user_email: str, websocket: WebSocket) -> bool:
    """
    Register a new connection. Returns False if limits exceeded.
    Thread-safe within a single worker.
    """
    async with _connection_lock:
        # Check global limit
        if len(_global_connections) >= MAX_GLOBAL_CONNECTIONS:
            logger.warning(f"Global connection limit reached ({MAX_GLOBAL_CONNECTIONS})")
            return False

        # Check per-user limit
        if len(_active_connections[user_email]) >= MAX_CONNECTIONS_PER_USER:
            logger.warning(f"User {user_email} connection limit reached ({MAX_CONNECTIONS_PER_USER})")
            return False

        _global_connections.add(websocket)
        _active_connections[user_email].add(websocket)
        logger.info(
            f"Registered connection for {user_email}. "
            f"User: {len(_active_connections[user_email])}, Global: {len(_global_connections)}"
        )
        return True


async def _unregister_connection(user_email: str, websocket: WebSocket) -> None:
    """Unregister a connection. Thread-safe within a single worker."""
    async with _connection_lock:
        _global_connections.discard(websocket)
        _active_connections[user_email].discard(websocket)
        # Clean up empty sets
        if user_email in _active_connections and not _active_connections[user_email]:
            del _active_connections[user_email]
        logger.debug(f"Unregistered connection for {user_email}")


class RateLimiter:
    """Simple token bucket rate limiter for log messages."""

    def __init__(self, rate: float, burst: int = 10):
        self.rate = rate  # messages per second
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = time.monotonic()  # Use monotonic instead of deprecated get_event_loop
        self._dropped_count = 0

    def acquire(self) -> tuple[bool, int]:
        """
        Try to acquire a token.

        Returns:
            Tuple of (allowed: bool, dropped_since_last_success: int)
        """
        now = time.monotonic()
        elapsed = now - self.last_update
        self.last_update = now

        # Add tokens based on time elapsed
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)

        if self.tokens >= 1:
            self.tokens -= 1
            dropped = self._dropped_count
            self._dropped_count = 0
            return True, dropped
        else:
            self._dropped_count += 1
            return False, 0


def _sanitize_log_line(line: str) -> str:
    """
    Sanitize log line to prevent injection attacks.

    - Escapes HTML entities to prevent XSS if client renders as HTML
    - Removes/escapes control characters
    - Limits line length
    """
    # Limit line length to prevent memory issues
    max_length = 10000
    if len(line) > max_length:
        line = line[:max_length] + "... [truncated]"

    # Escape HTML entities to prevent XSS
    line = html.escape(line)

    # Remove control characters except newline and tab
    line = "".join(char if char >= " " or char in "\t\n" else "?" for char in line)

    return line


@logs_websocket_router.websocket("/stream/{project_name}")
async def stream_logs(  # noqa: C901
    websocket: WebSocket,
    project_name: str,
    deployment: str = Query(..., description="Deployment name"),
    component: str = Query(..., description="Component reference name"),
    lines: int = Query(100, description="Initial historical lines", ge=1, le=1000),
) -> None:
    """
    WebSocket endpoint for streaming deployment logs in real-time.

    Requires session-based authentication (same as web UI).
    User must have access to the project.

    Args:
        project_name: Project name
        deployment: Deployment name within the project
        component: Component reference name
        lines: Number of historical log lines to retrieve initially
    """
    user_email: str | None = None
    process: asyncio.subprocess.Process | None = None
    connection_registered = False

    try:
        # === AUTHENTICATION ===
        # Extract session from cookies (same session as web UI)
        session = _get_session_from_cookie(websocket)
        user = _get_user_from_session(session)

        if not user or not user.get("email"):
            # Generic error to prevent information leakage
            logger.warning(f"WebSocket auth failed: no valid session for {project_name}")
            await websocket.close(code=4001, reason="Authentication required")
            return

        user_email = user["email"]
        logger.info(f"WebSocket auth: user {user_email} requesting logs for {project_name}/{deployment}/{component}")

        # Check if user is in allowed list
        user_service = get_user_service()
        if not user_service.is_email_allowed(user_email):
            # Generic error to prevent enumeration
            logger.warning(f"WebSocket auth failed: user {user_email} not allowed")
            await websocket.close(code=4003, reason="Access denied")
            return

        # === AUTHORIZATION ===
        project_service = get_project_service()

        # Check if user has access to this project
        if not project_service.is_user_authorized_for_project(project_name, user_email):
            # Generic error to prevent project enumeration
            logger.warning(f"WebSocket auth failed: user {user_email} not authorized for {project_name}")
            await websocket.close(code=4003, reason="Access denied")
            return

        # === CONNECTION LIMITS ===
        if not await _register_connection(user_email, websocket):
            logger.warning(f"WebSocket rejected: connection limit for {user_email}")
            await websocket.close(code=4029, reason="Too many connections")
            return

        connection_registered = True

        # Accept the connection after all security checks pass
        await websocket.accept()
        logger.info(f"WebSocket accepted for {user_email}: {project_name}/{deployment}/{component}")

        # === VALIDATION ===
        kubectl = KubectlConnector()
        current_cluster = settings.CLUSTER_MANAGER

        all_projects = project_service.get_all_projects()
        if project_name not in all_projects:
            await send_message(websocket, "error", message="Resource not found")
            await websocket.close(code=4004)
            return

        project_info = all_projects[project_name]
        project_data = project_info.data or {}
        deployments_list = project_data.get("deployments", [])

        # Find the deployment
        target_deployment = None
        for depl in deployments_list:
            if depl.get("name") == deployment:
                target_deployment = depl
                break

        if not target_deployment:
            await send_message(websocket, "error", message="Resource not found")
            await websocket.close(code=4004)
            return

        # Check if deployment is on current cluster
        if target_deployment.get("cluster") != current_cluster:
            await send_message(websocket, "error", message="Resource not available on this cluster")
            await websocket.close(code=4003)
            return

        # Find the component
        components = target_deployment.get("components", [])
        target_component = None
        for comp in components:
            if comp.get("reference") == component:
                target_component = comp
                break

        if not target_component:
            await send_message(websocket, "error", message="Resource not found")
            await websocket.close(code=4004)
            return

        # Get namespace and k8s deployment name
        namespace = get_prefixed_namespace(current_cluster, project_name)
        k8s_deployment_name = generate_unique_name(deployment, component)

        await send_message(
            websocket,
            "status",
            status="connected",
            message="Connected to log stream",
            deployment=deployment,
            component=component,
        )

        # === START LOG STREAMING ===
        # Rate limiter for outgoing messages
        rate_limiter = RateLimiter(rate=MAX_MESSAGES_PER_SECOND, burst=50)

        # Shared state for tasks
        current_component = component
        current_k8s_name = k8s_deployment_name
        sequence = 0
        paused = False
        running = True

        # Use a lock to coordinate process access between tasks
        process_lock = asyncio.Lock()

        process = await kubectl.stream_deployment_logs(
            deployment_name=current_k8s_name,
            namespace=namespace,
            lines=lines,
        )

        if process is None or process.stdout is None:
            await send_message(websocket, "error", message="Failed to start log stream")
            await websocket.close(code=5000)
            return

        await send_message(websocket, "status", status="streaming", message="Log streaming started")

        async def read_logs() -> None:
            """Read logs from kubectl process and send to client."""
            nonlocal sequence, running

            while running:
                # Get current process reference under lock, but release before I/O
                async with process_lock:
                    current_process = process
                    current_stdout = current_process.stdout if current_process else None

                if current_stdout is None:
                    await asyncio.sleep(0.1)
                    continue

                # Perform blocking I/O outside the lock
                try:
                    line = await asyncio.wait_for(current_stdout.readline(), timeout=0.5)
                except TimeoutError:
                    continue
                except Exception as e:
                    logger.debug(f"Error reading log line: {e}")
                    await asyncio.sleep(0.1)
                    continue

                if not line:
                    await asyncio.sleep(0.5)
                    continue

                if paused:
                    continue

                # Rate limiting with notification
                allowed, dropped = rate_limiter.acquire()
                if not allowed:
                    continue

                # Notify if messages were dropped
                if dropped > 0:
                    await send_message(
                        websocket,
                        "warning",
                        message=f"Rate limited: {dropped} log lines skipped",
                    )

                decoded_line = line.decode("utf-8", errors="replace").rstrip()
                sanitized_line = _sanitize_log_line(decoded_line)
                sequence += 1
                timestamp = datetime.now(UTC).isoformat()

                success = await send_message(
                    websocket,
                    "log",
                    deployment=deployment,
                    component=current_component,
                    line=sanitized_line,
                    timestamp=timestamp,
                    sequence=sequence,
                )

                if not success:
                    running = False
                    break

        # Separate rate limiter for stderr to prevent stderr flooding bypass
        stderr_rate_limiter = RateLimiter(rate=MAX_MESSAGES_PER_SECOND, burst=20)

        async def read_stderr() -> None:
            """Read and log stderr from kubectl process to prevent buffer deadlock."""
            nonlocal running

            while running:
                # Get current process reference under lock, but release before I/O
                async with process_lock:
                    current_process = process
                    current_stderr = current_process.stderr if current_process else None

                if current_stderr is None:
                    await asyncio.sleep(0.1)
                    continue

                # Perform blocking I/O outside the lock
                try:
                    line = await asyncio.wait_for(current_stderr.readline(), timeout=0.5)
                except TimeoutError:
                    continue
                except Exception:
                    await asyncio.sleep(0.1)
                    continue

                if line:
                    # Apply rate limiting to stderr as well
                    allowed, dropped = stderr_rate_limiter.acquire()
                    if not allowed:
                        continue

                    stderr_text = line.decode("utf-8", errors="replace").rstrip()
                    sanitized_text = _sanitize_log_line(stderr_text)
                    logger.warning(f"kubectl stderr: {sanitized_text}")

                    # Notify if stderr messages were dropped
                    if dropped > 0:
                        await send_message(
                            websocket,
                            "warning",
                            message=f"Rate limited: {dropped} stderr lines skipped",
                        )

                    await send_message(
                        websocket,
                        "log",
                        deployment=deployment,
                        component=current_component,
                        line=f"[STDERR] {sanitized_text}",
                        timestamp=datetime.now(UTC).isoformat(),
                        sequence=0,
                        level="error",
                    )

        async def handle_client_messages() -> None:
            """Handle incoming messages from client."""
            nonlocal paused, current_component, process, sequence, current_k8s_name, running

            while running:
                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    data = json.loads(message)
                    action = data.get("action")

                    if action == "pause":
                        paused = True
                        await send_message(websocket, "status", status="paused", message="Log streaming paused")

                    elif action == "resume":
                        paused = False
                        await send_message(websocket, "status", status="streaming", message="Log streaming resumed")

                    elif action == "switch":
                        new_component = data.get("component")
                        # Validate component name length to prevent memory issues
                        if new_component and len(new_component) > 256:
                            logger.warning("Component name too long in switch request")
                            await send_message(websocket, "error", message="Invalid component name")
                            continue
                        if new_component and new_component != current_component:
                            # Re-fetch project data to get current components
                            fresh_projects = project_service.get_all_projects()
                            if project_name not in fresh_projects:
                                await send_message(websocket, "error", message="Project no longer exists")
                                running = False
                                break

                            fresh_project = fresh_projects[project_name]
                            fresh_data = fresh_project.data or {}
                            fresh_deployments = fresh_data.get("deployments", [])

                            fresh_deployment = None
                            for depl in fresh_deployments:
                                if depl.get("name") == deployment:
                                    fresh_deployment = depl
                                    break

                            if not fresh_deployment:
                                await send_message(websocket, "error", message="Deployment no longer exists")
                                running = False
                                break

                            fresh_components = fresh_deployment.get("components", [])
                            new_target = None
                            for comp in fresh_components:
                                if comp.get("reference") == new_component:
                                    new_target = comp
                                    break

                            if not new_target:
                                await send_message(websocket, "error", message="Component not found")
                                continue

                            # Stop current process and start new one
                            async with process_lock:
                                if process:
                                    process.terminate()
                                    with contextlib.suppress(Exception):
                                        await asyncio.wait_for(process.wait(), timeout=2.0)

                                current_component = new_component
                                current_k8s_name = generate_unique_name(deployment, current_component)
                                sequence = 0

                                await send_message(
                                    websocket,
                                    "status",
                                    status="switching",
                                    message=f"Switching to {new_component}",
                                )

                                # Start new process
                                process = await kubectl.stream_deployment_logs(
                                    deployment_name=current_k8s_name,
                                    namespace=namespace,
                                    lines=lines,
                                )

                                if process and process.stdout:
                                    await send_message(
                                        websocket,
                                        "status",
                                        status="streaming",
                                        message=f"Now streaming {new_component}",
                                        component=new_component,
                                    )
                                else:
                                    await send_message(
                                        websocket, "error", message="Failed to start stream for component"
                                    )

                except TimeoutError:
                    continue
                except json.JSONDecodeError:
                    logger.warning("Received invalid JSON from client")
                    continue
                except WebSocketDisconnect:
                    running = False
                    break
                except Exception as e:
                    logger.error(f"Error handling client message: {e}")
                    running = False
                    break

        async def heartbeat() -> None:
            """Send periodic heartbeat to detect dead connections."""
            nonlocal running

            while running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if running:
                    success = await send_message(websocket, "heartbeat", timestamp=datetime.now(UTC).isoformat())
                    if not success:
                        logger.info("Heartbeat failed, closing connection")
                        running = False
                        break

        # Run all tasks concurrently
        log_task = asyncio.create_task(read_logs())
        stderr_task = asyncio.create_task(read_stderr())
        client_task = asyncio.create_task(handle_client_messages())
        heartbeat_task = asyncio.create_task(heartbeat())

        try:
            _done, pending = await asyncio.wait(
                [log_task, stderr_task, client_task, heartbeat_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Signal all tasks to stop
            running = False

            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        except asyncio.CancelledError:
            running = False
            log_task.cancel()
            stderr_task.cancel()
            client_task.cancel()
            heartbeat_task.cancel()

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {user_email}: {project_name}/{deployment}/{component}")

    except Exception as e:
        logger.exception(f"Error in WebSocket handler: {e}")
        with contextlib.suppress(Exception):
            await send_message(websocket, "error", message="Internal server error")

    finally:
        # Clean up subprocess
        if process:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            except Exception as e:
                logger.error(f"Error terminating kubectl process: {e}")

        # Unregister connection only if it was registered
        if connection_registered and user_email:
            await _unregister_connection(user_email, websocket)

        # Close WebSocket if still open
        with contextlib.suppress(Exception):
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()

        logger.info(f"WebSocket closed for {user_email}: {project_name}/{deployment}/{component}")
