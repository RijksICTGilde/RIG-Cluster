"""
WebSocket endpoint for real-time log streaming.

This module provides a WebSocket endpoint that streams deployment logs
in real-time using kubectl logs -f.
"""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.services.project_service import get_project_service
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

logs_websocket_router: APIRouter = APIRouter(
    prefix="/api/logs",
    tags=["logs-websocket"],
)


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

    Connects to kubectl logs -f and streams log lines to the client.

    Args:
        project_name: Project name
        deployment: Deployment name within the project
        component: Component reference name
        lines: Number of historical log lines to retrieve initially

    Message formats:
        Server -> Client:
            {"type": "log", "deployment": "...", "component": "...", "line": "...", "timestamp": "...", "sequence": 123}
            {"type": "status", "status": "streaming|paused|error|connected", "message": "..."}
            {"type": "error", "message": "..."}

        Client -> Server:
            {"action": "pause"} - Pause log streaming
            {"action": "resume"} - Resume log streaming
            {"action": "switch", "component": "new-component"} - Switch to different component
    """
    await websocket.accept()
    logger.info(f"WebSocket connection accepted for {project_name}/{deployment}/{component}")

    kubectl = KubectlConnector()
    current_cluster = settings.CLUSTER_MANAGER
    project_service = get_project_service()
    process = None
    sequence = 0
    paused = False

    try:
        # Validate project exists
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
        )

        # Start streaming logs
        process = await kubectl.stream_deployment_logs(
            deployment_name=k8s_deployment_name,
            namespace=namespace,
            lines=lines,
        )

        if process is None or process.stdout is None:
            await send_message(websocket, "error", message="Failed to start log stream")
            await websocket.close(code=5000)
            return

        await send_message(websocket, "status", status="streaming", message="Log streaming started")

        # Create tasks for reading logs and handling client messages
        async def read_logs() -> None:
            nonlocal sequence, paused
            try:
                while True:
                    if process.stdout is None:
                        break

                    line = await process.stdout.readline()
                    if not line:
                        # Process ended
                        break

                    if paused:
                        # Skip sending while paused, but keep reading to prevent buffer overflow
                        continue

                    decoded_line = line.decode("utf-8", errors="replace").rstrip()
                    sequence += 1
                    timestamp = datetime.now(UTC).isoformat()

                    success = await send_message(
                        websocket,
                        "log",
                        deployment=deployment,
                        component=component,
                        line=decoded_line,
                        timestamp=timestamp,
                        sequence=sequence,
                    )

                    if not success:
                        break

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error reading logs: {e}")

        async def handle_client_messages() -> None:  # noqa: C901
            nonlocal paused, component, process, sequence
            try:
                while True:
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
                            if new_component and new_component != component:
                                # Stop current stream
                                if process:
                                    process.terminate()
                                    await process.wait()

                                # Find new component
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

                                component = new_component
                                k8s_name = generate_unique_name(deployment, component)
                                sequence = 0

                                await send_message(
                                    websocket,
                                    "status",
                                    status="switching",
                                    message=f"Switching to {new_component}",
                                )

                                # Start new stream
                                process = await kubectl.stream_deployment_logs(
                                    deployment_name=k8s_name,
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
                                        websocket, "error", message=f"Failed to start stream for {new_component}"
                                    )

                    except TimeoutError:
                        # No message received, continue
                        continue
                    except json.JSONDecodeError:
                        logger.warning("Received invalid JSON from client")
                        continue

            except asyncio.CancelledError:
                pass
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.error(f"Error handling client messages: {e}")

        # Run both tasks concurrently
        log_task = asyncio.create_task(read_logs())
        client_task = asyncio.create_task(handle_client_messages())

        try:
            # Wait for either task to complete (or fail)
            _done, pending = await asyncio.wait(
                [log_task, client_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        except asyncio.CancelledError:
            log_task.cancel()
            client_task.cancel()

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {project_name}/{deployment}/{component}")

    except Exception as e:
        logger.exception(f"Error in WebSocket handler: {e}")
        try:
            await send_message(websocket, "error", message=str(e))
        except Exception:
            pass

    finally:
        # Clean up subprocess
        if process:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
            except Exception as e:
                logger.error(f"Error terminating kubectl process: {e}")

        # Close WebSocket if still open
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
        except Exception:
            pass

        logger.info(f"WebSocket connection closed for {project_name}/{deployment}/{component}")
