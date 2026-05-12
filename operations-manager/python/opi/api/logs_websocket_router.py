"""
WebSocket endpoint for real-time log streaming.

This module provides a WebSocket endpoint that streams deployment logs
in real-time using kubectl logs -f.

Security features:
- CSRF protection via Origin header validation
- Session-based authentication (same as web UI)
- Project-level authorization check
- Connection limits per user and globally
- Rate limiting on log messages
- Log content sanitization
- Client message size limits

Note on multi-worker deployments:
Connection limits are per-worker. For true global limits across workers,
use Redis or a shared state backend. Current implementation provides
per-worker protection which is sufficient for most deployments.
"""

import asyncio
import contextlib
import json
import logging
import time
from base64 import b64decode
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from itsdangerous import BadSignature, TimestampSigner
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
MAX_CLIENT_MESSAGE_SIZE = 1024  # 1KB max for client messages (actions like pause/resume/switch)

# Heartbeat interval
HEARTBEAT_INTERVAL_SECONDS = 30

# Bounded buffer sizes - caps memory usage from subprocess pipes
LOG_BUFFER_SIZE = 200  # Max log lines held in memory (sliding window)
STDERR_BUFFER_SIZE = 50  # Max stderr lines held in memory


def _validate_origin(websocket: WebSocket) -> bool:
    """
    Validate the Origin header to prevent Cross-Site WebSocket Hijacking (CSWSH).

    WebSocket connections are not subject to same-origin policy, so a malicious
    website could attempt to open a WebSocket connection using the victim's cookies.
    This validates that the Origin header matches our expected host.

    Returns:
        True if origin is valid or not provided (same-origin requests may omit it),
        False if origin is provided but doesn't match expected hosts.
    """
    origin = websocket.headers.get("origin", "")

    # If no origin header, it's likely a same-origin request or non-browser client
    # We allow this since authentication still applies
    if not origin:
        return True

    # Get the expected host from settings or from the request
    expected_host = websocket.headers.get("host", "")
    if not expected_host:
        # No host header - this shouldn't happen in valid HTTP
        logger.warning("WebSocket request missing Host header")
        return False

    # Build allowed origins (both http and https)
    # Note: In production, you may want to only allow https
    allowed_origins = [
        f"https://{expected_host}",
        f"http://{expected_host}",
    ]

    # Also allow configured external URL if set
    if hasattr(settings, "EXTERNAL_URL") and settings.EXTERNAL_URL:
        allowed_origins.append(settings.EXTERNAL_URL.rstrip("/"))

    if origin not in allowed_origins:
        logger.warning(f"WebSocket CSRF check failed: origin '{origin}' not in allowed list")
        return False

    return True


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
    Starlette uses itsdangerous.TimestampSigner with base64-encoded JSON.

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

        # Starlette's SessionMiddleware uses TimestampSigner (not URLSafeTimedSerializer)
        # The cookie value is: base64(json(session_data)) + signature
        signer = TimestampSigner(str(secret_key))

        try:
            # Unsign and validate with max_age
            data = signer.unsign(session_cookie.encode("utf-8"), max_age=SESSION_MAX_AGE_SECONDS)
            # Decode base64 and parse JSON
            session_data = json.loads(b64decode(data))
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
    """Token bucket rate limiter that paces (rather than drops) bursts.

    Originally this dropped any line that exceeded the burst, which silently
    truncated long startup tracebacks dumped in one go by ``kubectl logs``.
    The bucket now blocks the caller until a token is available, so every
    line is eventually delivered while sustained output is still capped at
    ``rate`` messages/sec.
    """

    def __init__(self, rate: float, burst: int = 10):
        self.rate = rate  # messages per second (sustained cap)
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = time.monotonic()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)

            if self.tokens >= 1:
                self.tokens -= 1
                return

            # Sleep just long enough for the next token to appear.
            deficit = 1 - self.tokens
            await asyncio.sleep(deficit / self.rate)


def _sanitize_log_line(line: str) -> str:
    """
    Sanitize log line to prevent injection attacks.

    - Removes control characters (except tab/newline)
    - Limits line length
    - Does NOT HTML-escape: the client uses textContent (safe) or escapeHtml() for highlighting

    Note: HTML escaping is intentionally NOT done here because the JavaScript client
    handles it safely using textContent for plain rendering or explicit escapeHtml()
    when adding search highlights. Double-escaping would cause display issues.
    """
    # Limit line length to prevent memory issues
    max_length = 10000
    if len(line) > max_length:
        line = line[:max_length] + "... [truncated]"

    # Remove control characters except newline and tab
    line = "".join(char if char >= " " or char in "\t\n" else "?" for char in line)

    return line


@logs_websocket_router.websocket("/stream/{project_name}")
async def stream_logs(
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
        # === CSRF PROTECTION ===
        # Validate Origin header to prevent Cross-Site WebSocket Hijacking
        if not _validate_origin(websocket):
            logger.warning(f"WebSocket rejected: CSRF check failed for {project_name}")
            await websocket.close(code=4003, reason="Access denied")
            return

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

        # Bounded queues act as sliding windows - drain tasks fill them,
        # reader tasks consume them. This prevents unbounded memory growth
        # from subprocess pipes when log volume exceeds WebSocket throughput.
        log_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=LOG_BUFFER_SIZE)
        stderr_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=STDERR_BUFFER_SIZE)

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

        # Pace reattach attempts so a permanently-broken pod (CrashLoopBackOff
        # where kubectl logs -f exits within milliseconds) doesn't have us
        # spawning kubectl every few seconds in a tight loop. We back off
        # exponentially up to a cap when consecutive attaches die immediately.
        REATTACH_MIN_INTERVAL_SECONDS = 5.0
        REATTACH_MAX_INTERVAL_SECONDS = 30.0
        REATTACH_QUICK_EXIT_THRESHOLD_SECONDS = 1.0
        last_reattach_at = 0.0
        consecutive_quick_exits = 0

        async def drain_stdout() -> None:
            """Drain subprocess stdout into bounded queue, dropping oldest when full.

            When the kubectl subprocess exits (which happens when a pod's last
            container terminates, e.g. CrashLoopBackOff between restarts), we
            reattach with a fresh `kubectl logs -f` instead of tearing down the
            WebSocket — that way the user keeps seeing logs across pod
            restarts, matching the Argo UI's behaviour.
            """
            nonlocal running, process, last_reattach_at, consecutive_quick_exits
            while running:
                async with process_lock:
                    current_process = process
                    current_stdout = current_process.stdout if current_process else None

                if current_stdout is None:
                    await asyncio.sleep(1.0)
                    continue

                try:
                    line = await asyncio.wait_for(current_stdout.readline(), timeout=2.0)
                except TimeoutError:
                    continue
                except Exception:
                    await asyncio.sleep(1.0)
                    continue

                if line:
                    # Sliding window: drop oldest line when queue is full
                    if log_queue.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            log_queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        log_queue.put_nowait(line)
                    continue

                # EOF on stdout — decide whether to reattach or just keep
                # looping (the snapshot may be a process that's already been
                # replaced by a component-switch).
                should_reattach = False
                exited_quickly = False
                async with process_lock:
                    if process is not None and process.returncode is not None:
                        # If kubectl exited within ~1s the matched pod has no
                        # active container (CrashLoopBackOff backoff window);
                        # back off so we don't spawn a fresh kubectl every 5s
                        # only to have it dump the same stored tail and exit.
                        exited_quickly = (time.monotonic() - last_reattach_at) < REATTACH_QUICK_EXIT_THRESHOLD_SECONDS
                        logger.info(
                            f"kubectl log stream for {current_k8s_name} exited "
                            f"(code {process.returncode}); will reattach "
                            f"(quick_exit={exited_quickly})"
                        )
                        process = None
                        should_reattach = True

                if not should_reattach:
                    await asyncio.sleep(0.5)
                    continue

                # Exponential backoff when consecutive attaches die instantly
                # (typically pod sitting in CrashLoopBackOff backoff window).
                if exited_quickly:
                    consecutive_quick_exits += 1
                else:
                    consecutive_quick_exits = 0
                backoff = min(
                    REATTACH_MIN_INTERVAL_SECONDS * (2 ** max(0, consecutive_quick_exits - 1)),
                    REATTACH_MAX_INTERVAL_SECONDS,
                )
                now = time.monotonic()
                wait = backoff - (now - last_reattach_at)
                if wait > 0:
                    await asyncio.sleep(wait)

                if not running:
                    break

                # Spawn the new follower OUTSIDE the lock so we don't block
                # stderr draining or component-switching during the kubectl
                # fork; then swap atomically. Use --tail=0 (no historical
                # dump) so each reattach only streams *new* output. Without
                # this, every backoff cycle would re-emit the same N lines
                # of the stored tail, drowning the WebSocket in duplicates.
                new_process = await kubectl.stream_deployment_logs(
                    deployment_name=current_k8s_name,
                    namespace=namespace,
                    lines=0,
                )
                last_reattach_at = time.monotonic()
                async with process_lock:
                    if not running or process is not None:
                        # Either we are shutting down, or a component switch
                        # already installed a fresh process — discard ours.
                        if new_process is not None:
                            with contextlib.suppress(ProcessLookupError):
                                new_process.terminate()
                    elif new_process is not None:
                        process = new_process
                        logger.info(f"Reattached log stream for {current_k8s_name} (PID {new_process.pid})")

        async def drain_stderr() -> None:
            """Drain subprocess stderr into bounded queue, dropping oldest when full."""
            while running:
                async with process_lock:
                    current_process = process
                    current_stderr = current_process.stderr if current_process else None

                if current_stderr is None:
                    await asyncio.sleep(1.0)
                    continue

                try:
                    line = await asyncio.wait_for(current_stderr.readline(), timeout=2.0)
                except TimeoutError:
                    continue
                except Exception:
                    await asyncio.sleep(1.0)
                    continue

                if not line:
                    # EOF on stderr pipe - check if subprocess has died
                    async with process_lock:
                        if process and process.returncode is not None:
                            logger.info(f"drain_stderr: subprocess exited with code {process.returncode}")
                            break
                    await asyncio.sleep(1.0)
                    continue

                if stderr_queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        stderr_queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    stderr_queue.put_nowait(line)

        async def read_logs() -> None:
            """Read logs from bounded queue and send to client."""
            nonlocal sequence, running

            while running:
                try:
                    line = await asyncio.wait_for(log_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue

                if paused:
                    continue

                # Pace output without dropping. Bursty traceback dumps from
                # ``kubectl logs`` are delivered in full; sustained output is
                # still capped at ``rate`` messages/sec.
                await rate_limiter.acquire()

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
            """Read stderr from bounded queue and forward warnings."""
            nonlocal running

            while running:
                try:
                    line = await asyncio.wait_for(stderr_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue

                await stderr_rate_limiter.acquire()

                stderr_text = line.decode("utf-8", errors="replace").rstrip()
                sanitized_text = _sanitize_log_line(stderr_text)
                logger.warning(f"kubectl stderr: {sanitized_text}")

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

                    # Validate message size to prevent memory exhaustion
                    if len(message) > MAX_CLIENT_MESSAGE_SIZE:
                        logger.warning(
                            f"Client message too large: {len(message)} bytes (max {MAX_CLIENT_MESSAGE_SIZE})"
                        )
                        await send_message(websocket, "error", message="Message too large")
                        continue

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
                            logger.info("switch: acquiring process_lock")
                            async with process_lock:
                                logger.info("switch: lock acquired")
                                if process:
                                    logger.info(f"switch: terminating PID {process.pid}")
                                    process.terminate()
                                    try:
                                        await asyncio.wait_for(process.wait(), timeout=2.0)
                                        logger.info("switch: process terminated cleanly")
                                    except OSError, TimeoutError:
                                        logger.warning(f"switch: terminate timeout, killing PID {process.pid}")
                                        with contextlib.suppress(OSError):
                                            process.kill()
                                            await asyncio.wait_for(process.wait(), timeout=5.0)
                                        logger.info("switch: process killed")

                                # Clear queues so old component logs don't bleed through
                                while not log_queue.empty():
                                    try:
                                        log_queue.get_nowait()
                                    except asyncio.QueueEmpty:
                                        break
                                while not stderr_queue.empty():
                                    try:
                                        stderr_queue.get_nowait()
                                    except asyncio.QueueEmpty:
                                        break

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
                                logger.info(f"switch: starting new stream for {current_k8s_name}")
                                process = await kubectl.stream_deployment_logs(
                                    deployment_name=current_k8s_name,
                                    namespace=namespace,
                                    lines=lines,
                                )
                                logger.info(f"switch: new process PID {process.pid if process else 'None'}")

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
                            logger.info("switch: process_lock released")

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

        # Run all tasks concurrently - drain tasks keep pipe buffers small,
        # reader tasks consume from bounded queues
        conn_id = f"{deployment}/{current_component}"
        stdout_drain_task = asyncio.create_task(drain_stdout(), name=f"drain_stdout:{conn_id}")
        stderr_drain_task = asyncio.create_task(drain_stderr(), name=f"drain_stderr:{conn_id}")
        log_task = asyncio.create_task(read_logs(), name=f"read_logs:{conn_id}")
        stderr_task = asyncio.create_task(read_stderr(), name=f"read_stderr:{conn_id}")
        client_task = asyncio.create_task(handle_client_messages(), name=f"client_msgs:{conn_id}")
        heartbeat_task = asyncio.create_task(heartbeat(), name=f"heartbeat:{conn_id}")

        all_tasks = [stdout_drain_task, stderr_drain_task, log_task, stderr_task, client_task, heartbeat_task]

        try:
            _done, pending = await asyncio.wait(
                all_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )

            done_names = [t.get_name() for t in _done]
            logger.info(f"task completed: {done_names}, cancelling {len(pending)} pending tasks")

            # Signal all tasks to stop
            running = False

            for task in pending:
                task_name = task.get_name()
                logger.debug(f"cancelling task: {task_name}")
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.CancelledError, TimeoutError:
                    if not task.done():
                        logger.error(f"task {task_name} did NOT cancel within 5s")

        except asyncio.CancelledError:
            running = False
            for task in all_tasks:
                task.cancel()

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {user_email}: {project_name}/{deployment}/{component}")

    except Exception as e:
        logger.exception(f"Error in WebSocket handler: {e}")
        with contextlib.suppress(OSError, RuntimeError):
            await send_message(websocket, "error", message="Internal server error")

    finally:
        # Clean up subprocess
        if process:
            logger.info(f"cleanup: terminating PID {process.pid}, returncode={process.returncode}")
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
                logger.info("cleanup: process terminated cleanly")
            except TimeoutError:
                logger.warning(f"cleanup: terminate timeout, killing PID {process.pid}")
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                    logger.info("cleanup: process killed")
                except TimeoutError:
                    logger.error(f"cleanup: KILL FAILED for PID {process.pid} - process may be stuck")
            except Exception as e:
                logger.error(f"cleanup: error terminating kubectl process: {e}")

        # Unregister connection only if it was registered
        if connection_registered and user_email:
            await _unregister_connection(user_email, websocket)

        # Close WebSocket if still open
        with contextlib.suppress(OSError, RuntimeError):
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()

        logger.info(f"WebSocket closed for {user_email}: {project_name}/{deployment}/{component}")
