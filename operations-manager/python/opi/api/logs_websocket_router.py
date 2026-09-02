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
from opi.api.logs_router import MAX_NAME_LENGTH, MAX_POD_NAME_LENGTH, resolve_component_pods
from opi.connectors.kubectl import KubectlConnector, is_previous_attempt_missing
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.manager.run_support import LABEL_RUN
from opi.services.project_authorization import (
    is_user_authorized_for_project,
)
from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service
from opi.utils.naming import generate_unique_name
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

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
# Idle session timeout for WebSocket handshakes, shared with the HTTP
# SessionMiddleware so both reject sessions that have been idle too long.
SESSION_MAX_AGE_SECONDS = settings.SESSION_MAX_AGE_SECONDS
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


async def _pod_belongs_to_component(
    kubectl: KubectlConnector,
    project_name: str,
    deployment_name: str,
    component: str,
    pod_name: str,
) -> bool:
    """Whether ``pod_name`` is one of this component's pods, and may therefore be followed.

    THIS IS THE WHOLE POINT OF THE POD PARAMETER'S SECURITY. A project namespace holds the
    deployments of everyone on the team; without this check a member who guessed a pod name
    could tail any of them, because ``kubectl logs <pod>`` does not care which component the
    pod belongs to. Asked of ``resolve_component_pods`` so the answer is the same one the
    pod-list endpoint gives -- a second implementation would be a second answer.

    Also applied on ``switch``, not only when the socket opens: a connection that switched
    component would otherwise keep a pod name that was validated against the previous one.
    """
    if not pod_name or len(pod_name) > MAX_POD_NAME_LENGTH:
        logger.warning("Rejecting pod name of %d characters for %s", len(pod_name), project_name)
        return False
    pods = await resolve_component_pods(
        kubectl,
        project_name=project_name,
        deployment_name=deployment_name,
        component=component,
    )
    if not pods:
        return False
    return any(candidate.get("name") == pod_name for candidate in pods)


def _retrieve_done_task_exceptions(done: set[asyncio.Task[Any]]) -> None:
    """Consume finished tasks' exceptions so asyncio does not later log
    "Future exception was never retrieved" when the Task is garbage-collected.

    A dropped client socket (e.g. keepalive ping timeout, close code 1011) makes
    the first task finish with ConnectionClosed / WebSocketDisconnect, which is a
    normal disconnect. Anything else is a genuine failure and is logged with a
    traceback.
    """
    for task in done:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionClosed)):
            logger.error(f"task {task.get_name()} failed: {exc!r}", exc_info=exc)


@logs_websocket_router.websocket("/stream/{project_name}")
async def stream_logs(
    websocket: WebSocket,
    project_name: str,
    deployment: str = Query(..., description="Deployment name"),
    component: str = Query(..., description="Component reference name"),
    lines: int = Query(250, description="Initial historical lines", ge=1, le=1000),
    pod: str | None = Query(None, description="One specific pod to follow, instead of every matching pod"),
    previous: bool = Query(False, description="Read the pod's previous attempt instead of the running container"),
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
        pod: A single pod to follow. Without it the label selector is used and the lines
            of EVERY matching pod arrive interleaved -- which is the behaviour that made a
            crashing pod unreadable next to a serving one. Never trusted: the name is
            checked against the pods of this project/deployment/component before it
            reaches a kubectl command, because the namespace holds the deployments of the
            whole team.
        previous: Read the pod's previous attempt. Only with ``pod``.
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
        user_email = user.get("email") if user else None

        if not user_email:
            # Generic error to prevent information leakage
            logger.warning(f"WebSocket auth failed: no valid session for {project_name}")
            await websocket.close(code=4001, reason="Authentication required")
            return

        logger.info(f"WebSocket auth: user {user_email} requesting logs for {project_name}/{deployment}/{component}")

        # Check if user is in allowed list
        user_service = get_user_service()
        if not user_service.is_email_allowed(user_email):
            # Generic error to prevent enumeration
            logger.warning(f"WebSocket auth failed: user {user_email} not allowed")
            await websocket.close(code=4003, reason="Access denied")
            return

        # === AUTHORIZATION ===

        # Check if user has access to this project
        if not is_user_authorized_for_project(project_name, user_email):
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

        project_info = get_project_store().get(project_name)
        if project_info is None:
            await send_message(websocket, "error", message="Resource not found")
            await websocket.close(code=4004)
            return

        project_data = project_info.data or {}
        deployments_list = project_data.get("deployments", [])

        # Find the deployment
        target_deployment = None
        for depl in deployments_list:
            if depl.get("name") == deployment:
                target_deployment = depl
                break

        namespace = get_prefixed_namespace(current_cluster, project_name)

        if target_deployment:
            # Normal deployment/component: validate cluster + component.
            if target_deployment.get("cluster") != current_cluster:
                await send_message(websocket, "error", message="Resource not available on this cluster")
                await websocket.close(code=4003)
                return

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

            k8s_deployment_name = generate_unique_name(deployment, component)

            # A pod name from the client only ever reaches kubectl after it has been
            # found among THIS component's pods. Same function the pod-list endpoint
            # uses, so the panel can never offer something the stream then refuses --
            # and, more to the point, so a guessed name is never followed.
            if pod is not None and not await _pod_belongs_to_component(
                kubectl, project_name, deployment, component, pod
            ):
                await send_message(websocket, "error", message="Resource not found")
                await websocket.close(code=4004)
                return
        else:
            # Ad-hoc run pod (database console / job): the `deployment` param is the
            # pod's `app` label. Only allow it if that pod is actually a run bundle
            # (carries the rig.zad/run label) in the user's own project namespace, so
            # a member cannot tail arbitrary pods by guessing an app label.
            run_pods = await kubectl.get_resources_by_label("pod", namespace, f"app={deployment},{LABEL_RUN}")
            if not run_pods:
                await send_message(websocket, "error", message="Resource not found")
                await websocket.close(code=4004)
                return
            k8s_deployment_name = deployment
            # The ad-hoc run path (database console, jobs) gets no pod selection: it is
            # already one pod, addressed by its own label, and it has no component to
            # validate a pod name against.
            pod = None
            previous = False

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
        current_pod = pod
        current_previous = bool(pod and previous)
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
            pod_name=current_pod,
            previous=current_previous,
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

        async def _restart_stream(*, tail: int) -> bool:
            """Stop the running follower and start a fresh one for the CURRENT selection.

            One place that does the terminate-drain-restart sequence, so the three callers
            -- a component switch, a pod switch, and the fall back out of the
            previous-attempt stand -- cannot each get a different part of it wrong. Leaving
            a queue unemptied bleeds the old pod's lines into the new one; leaving the old
            process running leaves a kubectl per switch.

            Returns whether a stream is running afterwards.
            """
            nonlocal process, sequence

            async with process_lock:
                if process:
                    logger.info(f"restart: terminating PID {process.pid}")
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except OSError, TimeoutError:
                        logger.warning(f"restart: terminate timeout, killing PID {process.pid}")
                        with contextlib.suppress(OSError):
                            process.kill()
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                    process = None

                # Clear queues so the previous selection's logs don't bleed through
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

                sequence = 0
                process = await kubectl.stream_deployment_logs(
                    deployment_name=current_k8s_name,
                    namespace=namespace,
                    lines=tail,
                    pod_name=current_pod,
                    previous=current_previous,
                )
                logger.info(f"restart: new process PID {process.pid if process else 'None'}")
                return bool(process and process.stdout)

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
                #
                # NIET in de vorige-poging-stand. Daar valt niets te volgen: de container
                # is al gestopt, de API levert het bewaarde logboek en kubectl eindigt
                # meteen. Opnieuw aanhaken zou datzelfde afgesloten logboek elke paar
                # seconden opnieuw dumpen. De lus blijft wel gewoon LEZEN -- deze
                # voorwaarde stond eerst boven aan de lus en sloeg daarmee de readline
                # zelf over, waardoor de vorige poging op de sandbox nul regels gaf
                # terwijl kubectl er twee had (gemeten 28 augustus 2026).
                should_reattach = False
                exited_quickly = False
                if current_previous:
                    await asyncio.sleep(0.5)
                    continue
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
                    pod_name=current_pod,
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

        # kubectl emits these on stderr whenever the upstream container
        # terminates while -f is following — which is expected (and frequent)
        # for CrashLoopBackOff pods, where every reattach cycle ends this
        # way. Forwarding them to the WebSocket added a "[STDERR] error:
        # unexpected EOF" line to the panel every 5-30 seconds, which the
        # user can't do anything about. Keep logging them server-side, but
        # don't pollute the client view.
        _BENIGN_KUBECTL_STDERR_SUBSTRINGS = ("unexpected EOF",)

        async def read_stderr() -> None:
            """Read stderr from bounded queue and forward warnings."""
            nonlocal running, current_previous

            while running:
                try:
                    line = await asyncio.wait_for(stderr_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue

                await stderr_rate_limiter.acquire()

                stderr_text = line.decode("utf-8", errors="replace").rstrip()
                sanitized_text = _sanitize_log_line(stderr_text)
                logger.warning(f"kubectl stderr: {sanitized_text}")

                if any(s in sanitized_text for s in _BENIGN_KUBECTL_STDERR_SUBSTRINGS):
                    continue

                # Er is geen vorige poging voor deze pod. Dat is geen storing en de rauwe
                # kubectl-tekst ("Error from server (BadRequest): previous terminated
                # container ... not found") leest wel als een. Zeg het in een zin en val
                # terug op de gewone stroom, zodat het paneel niet leeg blijft.
                if current_previous and is_previous_attempt_missing(sanitized_text):
                    current_previous = False
                    await send_message(
                        websocket,
                        "status",
                        status="streaming",
                        message="Deze pod heeft geen vorige poging; je kijkt naar de lopende stroom.",
                        previous=False,
                    )
                    await _restart_stream(tail=lines)
                    continue

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
            nonlocal current_pod, current_previous

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
                        new_component = data.get("component") or current_component
                        component_changes = new_component != current_component
                        # De podkeuze en de vorige-poging-stand horen bij dezelfde
                        # omschakeling: het paneel wisselt van pod zonder van component te
                        # wisselen, en de schakelaar "vorige poging" doet dat ook.
                        # Ontbreekt de sleutel, dan blijft de huidige stand staan -- BEHALVE
                        # bij een componentwissel, want een pod hoort bij een component. Hem
                        # daar laten staan zou een pod van het vorige component doorgeven,
                        # die de toets hieronder terecht weigert, en de gebruiker krijgt een
                        # foutmelding op een wissel die hij gewoon vroeg.
                        if "pod" in data:
                            new_pod = data["pod"]
                        elif component_changes:
                            new_pod = None
                        else:
                            new_pod = current_pod
                        new_previous = bool(data.get("previous", current_previous))
                        # Validate component name length to prevent memory issues
                        if new_component and len(new_component) > MAX_NAME_LENGTH:
                            logger.warning("Component name too long in switch request")
                            await send_message(websocket, "error", message="Invalid component name")
                            continue
                        if new_pod is not None and not isinstance(new_pod, str):
                            await send_message(websocket, "error", message="Invalid pod name")
                            continue

                        if component_changes:
                            # Re-fetch project data to get current components
                            fresh_project = get_project_store().get(project_name)
                            if fresh_project is None:
                                await send_message(websocket, "error", message="Project no longer exists")
                                running = False
                                break

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

                        elif new_pod == current_pod and new_previous == current_previous:
                            # Niets gewijzigd: geen omschakeling, geen leeg paneel.
                            continue

                        # Een podnaam van de client wordt ook HIER getoetst en niet alleen
                        # bij het openen: zonder dit kan iemand met een verbinding op zijn
                        # eigen component een pod van een collega noemen in een switch.
                        if new_pod is not None and not await _pod_belongs_to_component(
                            kubectl, project_name, deployment, new_component, new_pod
                        ):
                            await send_message(websocket, "error", message="Resource not found")
                            continue

                        current_component = new_component
                        current_k8s_name = generate_unique_name(deployment, current_component)
                        current_pod = new_pod
                        current_previous = bool(new_pod and new_previous)

                        await send_message(
                            websocket,
                            "status",
                            status="switching",
                            message=f"Switching to {new_pod or new_component}",
                        )

                        if await _restart_stream(tail=lines):
                            await send_message(
                                websocket,
                                "status",
                                status="streaming",
                                message=f"Now streaming {new_pod or new_component}",
                                component=new_component,
                                pod=current_pod,
                                previous=current_previous,
                            )
                        else:
                            await send_message(websocket, "error", message="Failed to start stream for component")

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

            _retrieve_done_task_exceptions(_done)

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
