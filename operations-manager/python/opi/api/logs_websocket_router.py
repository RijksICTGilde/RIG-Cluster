"""
WebSocket endpoint for real-time log streaming.

This module provides a WebSocket endpoint that streams deployment logs
in real-time using kubectl logs -f.

Security features:
- Session-based authentication (same as web UI)
- Project-level authorization check
- Connection limits per user and globally
- Rate limiting on log messages
"""

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from itsdangerous import URLSafeTimedSerializer
from starlette.websockets import WebSocketState

from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.services.project_service import get_project_service
from opi.services.user_service import get_user_service
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

logs_websocket_router: APIRouter = APIRouter(
    prefix="/api/logs",
    tags=["logs-websocket"],
)

# Connection tracking for rate limiting
_active_connections: dict[str, set[WebSocket]] = defaultdict(set)  # user_email -> set of websockets
_global_connections: set[WebSocket] = set()

# Limits
MAX_CONNECTIONS_PER_USER = 5
MAX_GLOBAL_CONNECTIONS = 100
MAX_MESSAGES_PER_SECOND = 100  # Rate limit for log messages


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

    This replicates the session extraction done by Starlette's SessionMiddleware
    for regular HTTP requests.

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

        # Decode the session using the same secret key as SessionMiddleware
        # Starlette uses itsdangerous for session signing
        secret_key = settings.SESSION_SECRET_KEY
        if not secret_key:
            logger.error("SESSION_SECRET_KEY not configured")
            return None

        serializer = URLSafeTimedSerializer(secret_key)
        # Session cookies don't have max_age validation in our case
        session_data = serializer.loads(session_cookie)

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


def _register_connection(user_email: str, websocket: WebSocket) -> bool:
    """
    Register a new connection. Returns False if limits exceeded.
    """
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
    logger.info(f"Registered connection for {user_email}. User: {len(_active_connections[user_email])}, Global: {len(_global_connections)}")
    return True


def _unregister_connection(user_email: str, websocket: WebSocket) -> None:
    """Unregister a connection."""
    _global_connections.discard(websocket)
    _active_connections[user_email].discard(websocket)
    # Clean up empty sets
    if not _active_connections[user_email]:
        del _active_connections[user_email]
    logger.debug(f"Unregistered connection for {user_email}")


class RateLimiter:
    """Simple token bucket rate limiter for log messages."""

    def __init__(self, rate: float, burst: int = 10):
        self.rate = rate  # messages per second
        self.burst = burst
        self.tokens = burst
        self.last_update = asyncio.get_event_loop().time()

    async def acquire(self) -> bool:
        """Try to acquire a token. Returns True if allowed, False if rate limited."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self.last_update
        self.last_update = now

        # Add tokens based on time elapsed
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)

        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


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

    try:
        # === AUTHENTICATION ===
        # Extract session from cookies (same session as web UI)
        session = _get_session_from_cookie(websocket)
        user = _get_user_from_session(session)

        if not user or not user.get("email"):
            logger.warning(f"WebSocket connection rejected: no authenticated user for {project_name}")
            await websocket.close(code=4001, reason="Authentication required")
            return

        user_email = user["email"]
        logger.info(f"WebSocket auth: user {user_email} requesting logs for {project_name}/{deployment}/{component}")

        # Check if user is in allowed list
        user_service = get_user_service()
        if not user_service.is_email_allowed(user_email):
            logger.warning(f"WebSocket connection rejected: user {user_email} not in allowlist")
            await websocket.close(code=4003, reason="Access denied")
            return

        # === AUTHORIZATION ===
        project_service = get_project_service()

        # Check if user has access to this project
        if not project_service.is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"WebSocket connection rejected: user {user_email} not authorized for project {project_name}")
            await websocket.close(code=4003, reason="Not authorized for this project")
            return

        # === CONNECTION LIMITS ===
        if not _register_connection(user_email, websocket):
            logger.warning(f"WebSocket connection rejected: connection limit exceeded for {user_email}")
            await websocket.close(code=4029, reason="Too many connections")
            return

        # Accept the connection after all security checks pass
        await websocket.accept()
        logger.info(f"WebSocket connection accepted for {user_email}: {project_name}/{deployment}/{component}")

        # === VALIDATION ===
        kubectl = KubectlConnector()
        current_cluster = settings.CLUSTER_MANAGER

        all_projects = project_service.get_all_projects()
        if project_name not in all_projects:
            await send_message(websocket, "error", message=f"Project '{project_name}' not found")
            await websocket.close(code=4004)
            return

        project_info = all_projects[project_name]
        project_data = project_info.data or {}
        deployments = project_data.get("deployments", [])

        # Find the deployment
        target_deployment = None
        for depl in deployments:
            if depl.get("name") == deployment:
                target_deployment = depl
                break

        if not target_deployment:
            await send_message(websocket, "error", message=f"Deployment '{deployment}' not found")
            await websocket.close(code=4004)
            return

        # Check if deployment is on current cluster
        if target_deployment.get("cluster") != current_cluster:
            cluster = target_deployment.get("cluster")
            await send_message(
                websocket,
                "error",
                message=f"Deployment '{deployment}' is on cluster '{cluster}', not '{current_cluster}'",
            )
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
            msg = f"Component '{component}' not found in deployment '{deployment}'"
            await send_message(websocket, "error", message=msg)
            await websocket.close(code=4004)
            return

        # Get namespace and k8s deployment name
        namespace = get_prefixed_namespace(current_cluster, project_name)
        k8s_deployment_name = generate_unique_name(deployment, component)

        await send_message(
            websocket,
            "status",
            status="connected",
            message=f"Connected to {k8s_deployment_name} in {namespace}",
            deployment=deployment,
            component=component,
            namespace=namespace,
            user=user_email,
        )

        # === START LOG STREAMING ===
        # Rate limiter for outgoing messages
        rate_limiter = RateLimiter(rate=MAX_MESSAGES_PER_SECOND, burst=50)

        # Event to signal component switch
        switch_event = asyncio.Event()
        current_component = component
        current_k8s_name = k8s_deployment_name

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

        sequence = 0
        paused = False
        running = True

        async def read_logs() -> None:
            """Read logs from kubectl process and send to client."""
            nonlocal sequence, process, current_k8s_name, running

            while running:
                # Check if we need to switch to a new process
                if switch_event.is_set():
                    switch_event.clear()
                    # Process was already switched in handle_client_messages
                    if process is None or process.stdout is None:
                        break
                    continue

                if process is None or process.stdout is None:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    # Use wait_for to allow checking switch_event periodically
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=0.5)
                except TimeoutError:
                    continue
                except Exception as e:
                    logger.debug(f"Error reading log line: {e}")
                    break

                if not line:
                    # Process ended, wait a bit and check if we should continue
                    await asyncio.sleep(0.5)
                    continue

                if paused:
                    continue

                # Rate limiting
                if not await rate_limiter.acquire():
                    # Skip this message if rate limited
                    continue

                decoded_line = line.decode("utf-8", errors="replace").rstrip()
                sequence += 1
                timestamp = datetime.now(UTC).isoformat()

                success = await send_message(
                    websocket,
                    "log",
                    deployment=deployment,
                    component=current_component,
                    line=decoded_line,
                    timestamp=timestamp,
                    sequence=sequence,
                )

                if not success:
                    running = False
                    break

        async def read_stderr() -> None:
            """Read and log stderr from kubectl process to prevent buffer deadlock."""
            nonlocal process, running

            while running:
                if process is None or process.stderr is None:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    line = await asyncio.wait_for(process.stderr.readline(), timeout=0.5)
                    if line:
                        stderr_text = line.decode("utf-8", errors="replace").rstrip()
                        logger.warning(f"kubectl stderr: {stderr_text}")
                        # Optionally send stderr to client as well
                        await send_message(
                            websocket,
                            "log",
                            deployment=deployment,
                            component=current_component,
                            line=f"[STDERR] {stderr_text}",
                            timestamp=datetime.now(UTC).isoformat(),
                            sequence=0,
                            level="error",
                        )
                except TimeoutError:
                    continue
                except Exception:
                    break

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
                        if new_component and new_component != current_component:
                            # Validate new component exists
                            new_target = None
                            for comp in components:
                                if comp.get("reference") == new_component:
                                    new_target = comp
                                    break

                            if not new_target:
                                await send_message(
                                    websocket, "error", message=f"Component '{new_component}' not found"
                                )
                                continue

                            # Stop current process
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
                                # Signal the read_logs task to use new process
                                switch_event.set()
                                await send_message(
                                    websocket,
                                    "status",
                                    status="streaming",
                                    message=f"Now streaming {new_component}",
                                    component=new_component,
                                )
                            else:
                                await send_message(
                                    websocket, "error", message=f"Failed to start stream for {new_component}"
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

        # Run all tasks concurrently
        log_task = asyncio.create_task(read_logs())
        stderr_task = asyncio.create_task(read_stderr())
        client_task = asyncio.create_task(handle_client_messages())

        try:
            _done, pending = await asyncio.wait(
                [log_task, stderr_task, client_task],
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

        # Unregister connection
        if user_email:
            _unregister_connection(user_email, websocket)

        # Close WebSocket if still open
        with contextlib.suppress(Exception):
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()

        logger.info(f"WebSocket connection closed for {user_email}: {project_name}/{deployment}/{component}")
