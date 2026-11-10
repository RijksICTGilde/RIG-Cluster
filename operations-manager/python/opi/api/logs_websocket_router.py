"""The WebSocket that streams the logs of a deployment, through ``kubectl logs -f``.

What guards the socket: Origin validation against Cross-Site WebSocket Hijacking, the
portal session, a project authorization check, connection limits, a rate limit on the
messages, sanitization of every line, and a size limit on a client message.

The connection limits are per worker: there is no shared state between workers, so a
cluster-wide limit would need a backend of its own.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

from opi.api.logs_router import MAX_NAME_LENGTH, MAX_POD_NAME_LENGTH, resolve_component_pods
from opi.connectors.kubectl import KubectlConnector, is_previous_attempt_missing
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.http.auth.session_guard import valid_session_user
from opi.manager.run_support import LABEL_RUN
from opi.services.project import Project
from opi.services.project_authorization import (
    is_user_authorized_for_project,
)
from opi.services.project_store import get_project_store
from opi.services.step_up import session_step_up_until, step_up_fresh
from opi.services.user_service import get_user_service
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

logs_websocket_router: APIRouter = APIRouter(
    prefix="/api/logs",
    tags=["logs-websocket"],
)

# Per-worker state, so every limit below counts per worker.
_active_connections: dict[str, set[WebSocket]] = defaultdict(set)  # user_email -> set of websockets
_global_connections: set[WebSocket] = set()
_connection_lock = asyncio.Lock()

MAX_CONNECTIONS_PER_USER = 5
MAX_GLOBAL_CONNECTIONS = 100
MAX_MESSAGES_PER_SECOND = 100  # Rate limit for log messages
MAX_CLIENT_MESSAGE_SIZE = 1024  # 1KB max for client messages (actions like pause/resume/switch)

HEARTBEAT_INTERVAL_SECONDS = 30

# Bounded buffers, so a subprocess pipe cannot grow memory without a cap.
LOG_BUFFER_SIZE = 200  # Max log lines held in memory (sliding window)
STDERR_BUFFER_SIZE = 50  # Max stderr lines held in memory


def _validate_origin(websocket: WebSocket) -> bool:
    """Whether the Origin header is one of ours, against Cross-Site WebSocket Hijacking.

    A WebSocket is not subject to the same-origin policy, so any site could open one with
    the victim's cookies. True when the origin matches or is absent (a same-origin request
    may omit it), False when an origin is given that is not ours.
    """
    origin = websocket.headers.get("origin", "")

    # No origin is a same-origin request or a non-browser client, and authentication still
    # applies to it.
    if not origin:
        return True

    expected_host = websocket.headers.get("host", "")
    if not expected_host:
        logger.warning("WebSocket request missing Host header")
        return False

    allowed_origins = [
        f"https://{expected_host}",
        f"http://{expected_host}",
    ]

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


def _get_session_from_scope(websocket: WebSocket) -> Any:
    """The session the ServerSideSessionMiddleware already loaded, out of the scope.

    No session, or an empty one, answers None.
    """
    session = websocket.scope.get("session")
    if not session:
        logger.debug("No session found in WebSocket scope")
        return None
    return session


async def _register_connection(user_email: str, websocket: WebSocket) -> bool:
    """Register a new connection; False when a limit is reached. Safe within one worker."""
    async with _connection_lock:
        if len(_global_connections) >= MAX_GLOBAL_CONNECTIONS:
            logger.warning(f"Global connection limit reached ({MAX_GLOBAL_CONNECTIONS})")
            return False

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
    """Unregister a connection. Safe within one worker."""
    async with _connection_lock:
        _global_connections.discard(websocket)
        _active_connections[user_email].discard(websocket)
        if user_email in _active_connections and not _active_connections[user_email]:
            del _active_connections[user_email]
        logger.debug(f"Unregistered connection for {user_email}")


class RateLimiter:
    """Token bucket rate limiter that paces a burst rather than dropping it.

    ``acquire`` blocks until a token is free, so every line is delivered eventually and a
    long traceback that ``kubectl logs`` dumps in one go is not truncated, while sustained
    output stays capped at ``rate`` messages per second.
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
    """Drop the control characters from a log line and cap its length.

    Deliberately NO HTML escaping: the client renders with ``textContent`` and escapes by
    hand where it adds search highlights, so escaping here would double-escape.
    """
    max_length = 10000
    if len(line) > max_length:
        line = line[:max_length] + "... [truncated]"

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
    pod-list endpoint gives; a second implementation would be a second answer.

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
    """Consume the exception of every finished task, so asyncio does not log
    "Future exception was never retrieved" when the Task is garbage-collected.

    A dropped client socket (a keepalive ping timeout, close code 1011) ends the first task
    with ConnectionClosed or WebSocketDisconnect, which is a normal disconnect. Anything
    else is a genuine failure and is logged with a traceback.
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
    """Stream the logs of one deployment component, on the portal session.

    The caller needs a valid session (the same one as the web UI) and access to the project.

    Without ``pod`` the label selector is used and the lines of EVERY matching pod arrive
    interleaved, which is what made a crashing pod unreadable next to a serving one. A pod
    name from the client is never trusted: it is checked against the pods of this
    project/deployment/component before it reaches a kubectl command, because the namespace
    holds the deployments of the whole team. ``previous`` only works together with ``pod``.
    """
    user_email: str | None = None
    process: asyncio.subprocess.Process | None = None
    connection_registered = False

    try:
        if not _validate_origin(websocket):
            logger.warning(f"WebSocket rejected: CSRF check failed for {project_name}")
            await websocket.close(code=4003, reason="Access denied")
            return

        session = _get_session_from_scope(websocket)
        # The same check as the HTTP middleware: absolute lifetime and revocation.
        user = valid_session_user(session)
        user_email = user.get("email") if user else None

        if not user_email:
            logger.warning(f"WebSocket auth failed: no valid session for {project_name}")
            await websocket.close(code=4001, reason="Authentication required")
            return

        logger.info(f"WebSocket auth: user {user_email} requesting logs for {project_name}/{deployment}/{component}")

        user_service = get_user_service()
        if not user_service.is_email_allowed(user_email):
            # Generic error, so a refusal cannot be used to enumerate.
            logger.warning(f"WebSocket auth failed: user {user_email} not allowed")
            await websocket.close(code=4003, reason="Access denied")
            return

        # Checked once, on connecting: the stream outlives the window, like any open page.
        step_up_fresh.set(session_step_up_until(session) is not None)

        if not is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"WebSocket auth failed: user {user_email} not authorized for {project_name}")
            await websocket.close(code=4003, reason="Access denied")
            return

        if not await _register_connection(user_email, websocket):
            logger.warning(f"WebSocket rejected: connection limit for {user_email}")
            await websocket.close(code=4029, reason="Too many connections")
            return

        connection_registered = True

        # Only once every check above passed.
        await websocket.accept()
        logger.info(f"WebSocket accepted for {user_email}: {project_name}/{deployment}/{component}")

        kubectl = KubectlConnector()
        current_cluster = settings.CLUSTER_MANAGER

        project_info = get_project_store().get(project_name)
        if project_info is None:
            await send_message(websocket, "error", message="Resource not found")
            await websocket.close(code=4004)
            return

        project_data = project_info.data or {}
        target_deployment = Project(project_data).find("deployments", name=deployment)

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

            # A pod name from the client only ever reaches kubectl after it has been found
            # among THIS component's pods. The same function the pod-list endpoint uses, so
            # the panel cannot offer what the stream then refuses, and above all so a
            # guessed name is never followed.
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

        rate_limiter = RateLimiter(rate=MAX_MESSAGES_PER_SECOND, burst=50)

        # Shared between the tasks below.
        current_component = component
        current_k8s_name = k8s_deployment_name
        current_pod = pod
        current_previous = bool(pod and previous)
        sequence = 0
        paused = False
        running = True

        # Bounded queues as sliding windows: the drain tasks fill them and the reader tasks
        # consume them, so a log volume above the WebSocket throughput cannot grow memory.
        log_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=LOG_BUFFER_SIZE)
        stderr_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=STDERR_BUFFER_SIZE)

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

        # Reattach attempts are paced, so a permanently broken pod (CrashLoopBackOff, where
        # kubectl logs -f exits within milliseconds) does not have us spawning kubectl in a
        # tight loop. Consecutive attaches that die at once back off up to the cap.
        reattach_min_interval_seconds = 5.0
        reattach_max_interval_seconds = 30.0
        reattach_quick_exit_threshold_seconds = 1.0
        last_reattach_at = 0.0
        consecutive_quick_exits = 0

        async def _restart_stream(*, tail: int) -> bool:
            """Stop the running follower and start a fresh one for the CURRENT selection.

            One place that does the terminate-drain-restart sequence, so the three callers (a
            component switch, a pod switch, and the fall back out of the previous-attempt
            stand) cannot each get a different part of it wrong. Leaving a queue unemptied
            bleeds the old pod's lines into the new one; leaving the old process running
            leaves a kubectl per switch.

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
            """Drain subprocess stdout into the bounded queue, dropping the oldest when full.

            When the kubectl subprocess exits, which happens when a pod's last container
            terminates (CrashLoopBackOff between restarts), this reattaches with a fresh
            ``kubectl logs -f`` instead of tearing down the WebSocket, so the user keeps
            seeing logs across pod restarts.
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
                    if log_queue.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            log_queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        log_queue.put_nowait(line)
                    continue

                # EOF on stdout: reattach, or keep looping because the snapshot may be a
                # process a component switch already replaced.
                #
                # NOT in the previous-attempt stand. There is nothing to follow there: the
                # container has stopped, the API hands over the stored log and kubectl ends
                # at once, so reattaching would dump that same closed log every few seconds.
                # The loop does keep READING, and this condition therefore sits here and not
                # at the top of the loop, where it would skip the readline itself and the
                # previous attempt would yield no lines at all.
                should_reattach = False
                exited_quickly = False
                if current_previous:
                    await asyncio.sleep(0.5)
                    continue
                async with process_lock:
                    if process is not None and process.returncode is not None:
                        # kubectl exiting within ~1s means the matched pod has no active
                        # container (a CrashLoopBackOff backoff window), so backing off keeps
                        # a fresh kubectl from dumping the same stored tail every 5s.
                        exited_quickly = (time.monotonic() - last_reattach_at) < reattach_quick_exit_threshold_seconds
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

                if exited_quickly:
                    consecutive_quick_exits += 1
                else:
                    consecutive_quick_exits = 0
                backoff = min(
                    reattach_min_interval_seconds * (2 ** max(0, consecutive_quick_exits - 1)),
                    reattach_max_interval_seconds,
                )
                now = time.monotonic()
                wait = backoff - (now - last_reattach_at)
                if wait > 0:
                    await asyncio.sleep(wait)

                if not running:
                    break

                # The new follower is spawned OUTSIDE the lock, so the kubectl fork does not
                # block stderr draining or a component switch, and is swapped in atomically.
                # With lines=0, so each reattach streams only NEW output: otherwise every
                # backoff cycle re-emits the same stored tail.
                new_process = await kubectl.stream_deployment_logs(
                    deployment_name=current_k8s_name,
                    namespace=namespace,
                    lines=0,
                    pod_name=current_pod,
                )
                last_reattach_at = time.monotonic()
                async with process_lock:
                    if not running or process is not None:
                        # Either this is shutting down, or a component switch already
                        # installed a fresh process, so ours is discarded.
                        if new_process is not None:
                            with contextlib.suppress(ProcessLookupError):
                                new_process.terminate()
                    elif new_process is not None:
                        process = new_process
                        logger.info(f"Reattached log stream for {current_k8s_name} (PID {new_process.pid})")

        async def drain_stderr() -> None:
            """Drain subprocess stderr into the bounded queue, dropping the oldest when full."""
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
            nonlocal sequence, running

            while running:
                try:
                    line = await asyncio.wait_for(log_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue

                if paused:
                    continue

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

        # Its own limiter, so a flood on stderr cannot get around the one above.
        stderr_rate_limiter = RateLimiter(rate=MAX_MESSAGES_PER_SECOND, burst=20)

        # kubectl writes this on stderr whenever the upstream container terminates while -f
        # is following, which is normal and frequent for a CrashLoopBackOff pod: every
        # reattach cycle ends that way. It stays in the server log and out of the panel,
        # where the user can do nothing with it.
        benign_kubectl_stderr_substrings = ("unexpected EOF",)

        async def read_stderr() -> None:
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

                if any(s in sanitized_text for s in benign_kubectl_stderr_substrings):
                    continue

                # There is no previous attempt for this pod. That is not a failure, while the
                # raw kubectl text ("Error from server (BadRequest): previous terminated
                # container ... not found") reads like one, so this says it in a sentence and
                # falls back on the ordinary stream rather than leaving the panel empty.
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
            nonlocal paused, current_component, process, sequence, current_k8s_name, running
            nonlocal current_pod, current_previous

            while running:
                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)

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
                        # The pod choice and the previous-attempt stand belong to this same
                        # switch: the panel changes pod without changing component, and so
                        # does the "previous attempt" toggle. A missing key keeps the current
                        # stand, EXCEPT on a component switch, because a pod belongs to a
                        # component: keeping it there would pass on a pod of the previous
                        # component, which the check below rightly refuses, and the user would
                        # get an error on a switch they simply asked for.
                        if "pod" in data:
                            new_pod = data["pod"]
                        elif component_changes:
                            new_pod = None
                        else:
                            new_pod = current_pod
                        new_previous = bool(data.get("previous", current_previous))
                        if new_component and len(new_component) > MAX_NAME_LENGTH:
                            logger.warning("Component name too long in switch request")
                            await send_message(websocket, "error", message="Invalid component name")
                            continue
                        if new_pod is not None and not isinstance(new_pod, str):
                            await send_message(websocket, "error", message="Invalid pod name")
                            continue

                        if component_changes:
                            fresh_project = get_project_store().get(project_name)
                            if fresh_project is None:
                                await send_message(websocket, "error", message="Project no longer exists")
                                running = False
                                break

                            fresh_data = fresh_project.data or {}
                            fresh_deployment = Project(fresh_data).find("deployments", name=deployment)

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
                            # Nothing changed: no switch, and no empty panel.
                            continue

                        # A pod name from the client is checked HERE as well and not only when
                        # the socket opens: without it, someone with a connection on their own
                        # component could name a colleague's pod in a switch.
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
            """A dead connection shows up as a heartbeat that cannot be sent."""
            nonlocal running

            while running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if running:
                    success = await send_message(websocket, "heartbeat", timestamp=datetime.now(UTC).isoformat())
                    if not success:
                        logger.info("Heartbeat failed, closing connection")
                        running = False
                        break

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

        if connection_registered and user_email:
            await _unregister_connection(user_email, websocket)

        with contextlib.suppress(OSError, RuntimeError):
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()

        logger.info(f"WebSocket closed for {user_email}: {project_name}/{deployment}/{component}")
