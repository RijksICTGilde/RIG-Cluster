"""
Simple task tracking for FastAPI BackgroundTasks.
"""

# TODO: make the task manager actually add and finish tasks where they are created and done
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

# Functions imported locally in functions to avoid potential circular imports
from opi.connectors.git import GitConnector
from opi.core.config import settings

# ProjectManager imported locally to avoid circular import

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Task status enumeration."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """Simple task with status."""

    id: str
    name: str
    status: TaskStatus = TaskStatus.RUNNING
    parent_id: str | None = None  # If this is a subtask
    created_at: datetime = datetime.now()
    completed_at: datetime | None = None
    error: str | None = None


@dataclass
class ProjectInfo:
    """Project information for the progress page."""

    id: str
    project_name: str
    status: TaskStatus
    created_at: datetime
    current_step: str = "Starting..."
    logs: list[str] | None = None
    events: list[dict[str, str]] | None = None
    namespace: str | None = None
    web_addresses: dict[str, str] | None = None  # component_name -> web_address


# Simple in-memory storage for projects only
_projects: dict[str, ProjectInfo] = {}
# Store TaskProgressManager instances per project
_project_managers: dict[str, "TaskProgressManager"] = {}


class TaskProgressManager:
    """Task progress manager with clean API - one per project."""

    def __init__(self, project_id: str, project_name: str):
        self.project_id = project_id
        self.project_name = project_name
        self.tasks: dict[str, Task] = {}  # Local tasks for this project only

        # Create project info
        _projects[project_id] = ProjectInfo(
            id=project_id, project_name=project_name, status=TaskStatus.RUNNING, created_at=datetime.now()
        )

        # Store this manager instance
        _project_managers[project_id] = self
        logger.info(f"Created TaskProgressManager for project {project_name} ({project_id})")

    def add_task(self, name: str) -> str:
        """Add a task and start it immediately. Returns task ID."""
        task_id = str(uuid.uuid4())
        task = Task(id=task_id, name=name, status=TaskStatus.RUNNING)
        self.tasks[task_id] = task
        logger.info(f"Project {self.project_id}: Added task: {name} ({task_id})")
        self.update_current_step(name)
        return task_id

    def add_subtask(self, parent_task_id: str, name: str) -> str:
        """Add a subtask and start it immediately. Returns subtask ID."""
        subtask_id = str(uuid.uuid4())
        subtask = Task(id=subtask_id, name=name, status=TaskStatus.RUNNING, parent_id=parent_task_id)
        self.tasks[subtask_id] = subtask
        logger.info(f"Project {self.project_id}: Added subtask: {name} ({subtask_id}) under {parent_task_id}")
        self.update_current_step(name)
        return subtask_id

    def complete_task(self, task_id: str) -> None:
        """Mark a task as completed."""
        if task_id in self.tasks:
            self.tasks[task_id].status = TaskStatus.COMPLETED
            self.tasks[task_id].completed_at = datetime.now()
            logger.info(f"Project {self.project_id}: Completed task: {self.tasks[task_id].name} ({task_id})")

    def fail_task(self, task_id: str, error: str) -> None:
        """Mark a task as failed."""
        if task_id in self.tasks:
            self.tasks[task_id].status = TaskStatus.FAILED
            self.tasks[task_id].error = error
            self.tasks[task_id].completed_at = datetime.now()
            logger.error(f"Project {self.project_id}: Failed task: {self.tasks[task_id].name} ({task_id}): {error}")

    def update_current_step(self, step: str) -> None:
        """Update the current step for the project."""
        if self.project_id in _projects:
            _projects[self.project_id].current_step = step

    def complete_project(self) -> None:
        """Mark the entire project as completed."""
        if self.project_id in _projects:
            _projects[self.project_id].status = TaskStatus.COMPLETED

    def fail_project(self, error: str) -> None:
        """Mark the entire project as failed."""
        if self.project_id in _projects:
            _projects[self.project_id].status = TaskStatus.FAILED

    def set_namespace(self, namespace: str) -> None:
        """Set namespace for monitoring and start monitoring."""
        if self.project_id in _projects:
            _projects[self.project_id].namespace = namespace
            logger.info(f"Set namespace {namespace} for project {self.project_id}")
            # Automatically start monitoring when namespace is set
            self.start_monitoring()

    def add_logs(self, logs: list[str]) -> None:
        """Add logs for the project."""
        if self.project_id in _projects:
            _projects[self.project_id].logs = logs

    def add_events(self, events: list[dict[str, str]]) -> None:
        """Add events for the project."""
        if self.project_id in _projects:
            _projects[self.project_id].events = events

    def start_monitoring(self) -> None:
        """Start background monitoring for logs and events."""
        if self.project_id in _projects and _projects[self.project_id].namespace:
            # Start the monitoring task
            import asyncio

            asyncio.create_task(_monitor_project_progress(self.project_id))
            logger.info(
                f"Started monitoring for project {self.project_id} in namespace {_projects[self.project_id].namespace}"
            )
        else:
            logger.warning(f"Cannot start monitoring for project {self.project_id}: namespace not set")

    def start_subtask(self, subtask_id: str) -> None:
        """Mark subtask as started."""
        start_subtask(self.project_id, subtask_id)

    def complete_subtask(self, subtask_id: str) -> None:
        """Mark subtask as completed."""
        complete_subtask(self.project_id, subtask_id)

    def fail_subtask(self, subtask_id: str, error: str) -> None:
        """Mark subtask as failed."""
        fail_subtask(self.project_id, subtask_id, error)

    def update_component_deployment(self, component_name: str, deployment_name: str) -> None:
        """Update the deployment name for a component."""
        update_component_deployment(self.project_id, component_name, deployment_name)

    def update_component_web_address(self, component_name: str, web_address: str) -> None:
        """Update the web address for a component."""
        if self.project_id in _projects:
            if _projects[self.project_id].web_addresses is None:
                _projects[self.project_id].web_addresses = {}
            _projects[self.project_id].web_addresses[component_name] = web_address
            logger.info(f"Project {self.project_id}: Updated web address for {component_name}: {web_address}")
        # Also call the legacy function for compatibility
        update_component_web_address(self.project_id, component_name, web_address)

    def update_component_readiness(self, component_name: str, deployment_ready: str) -> None:
        """Update the deployment readiness status for a component."""
        update_component_readiness(self.project_id, component_name, deployment_ready)


def create_task(project_name: str) -> str:
    """Create a new project and return its ID."""
    project_id = str(uuid.uuid4())
    # Create project info in the new system
    _projects[project_id] = ProjectInfo(
        id=project_id, project_name=project_name, status=TaskStatus.RUNNING, created_at=datetime.now()
    )
    logger.info(f"Created project {project_id} for project {project_name}")
    return project_id


def get_task(task_id: str) -> ProjectInfo | None:
    """Get project information by ID."""
    return _projects.get(task_id)


def update_progress(task_id: str, progress: int, step: str) -> None:
    """Update task progress."""
    if task_id in _projects:
        _projects[task_id].current_step = step
        logger.debug(f"Task {task_id}: {progress}% - {step}")
    else:
        logger.debug(f"Task {task_id}: {progress}% - {step} (task not found)")


def complete_task(task_id: str, result: dict[str, Any]) -> None:
    """Mark task as completed."""
    if task_id in _projects:
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].current_step = "Completed"
        logger.info(f"Task {task_id} completed")
    else:
        logger.info(f"Task {task_id} completed (task not found)")


def fail_task(task_id: str, error: str) -> None:
    """Mark task as failed."""
    if task_id in _projects:
        _projects[task_id].status = TaskStatus.FAILED
        _projects[task_id].current_step = "Failed"
        logger.error(f"Task {task_id} failed: {error}")
    else:
        logger.error(f"Task {task_id} failed: {error} (task not found)")


def set_task_namespace(task_id: str, namespace: str) -> None:
    """Set the namespace for a task."""
    if task_id in _projects:
        _projects[task_id].namespace = namespace
        logger.debug(f"Task {task_id}: set namespace to {namespace}")
    else:
        logger.debug(f"Task {task_id}: set namespace to {namespace} (task not found)")


def add_component_status(task_id: str, component_name: str) -> None:
    """Add a component status to track for a task. (Legacy function - no-op in new system)"""
    logger.debug(f"Task {task_id}: component status add called for {component_name} - no-op in new system")


def update_component_deployment(task_id: str, component_name: str, deployment_name: str) -> None:
    """Update the deployment name for a component. (Legacy function - no-op in new system)"""
    logger.debug(
        f"Task {task_id}: component deployment update called (component: {component_name}, deployment: {deployment_name}) - no-op in new system"
    )


def update_component_web_address(task_id: str, component_name: str, web_address: str) -> None:
    """Update the web address for a component. (Legacy function - no-op in new system)"""
    logger.debug(
        f"Task {task_id}: component web address update called (component: {component_name}, address: {web_address}) - no-op in new system"
    )


def update_component_readiness(task_id: str, component_name: str, deployment_ready: str) -> None:
    """Update the deployment readiness status for a component. (Legacy function - no-op in new system)"""
    logger.debug(
        f"Task {task_id}: component readiness update called (component: {component_name}, ready: {deployment_ready}) - no-op in new system"
    )


def update_task_logs(task_id: str, logs: list[str]) -> None:
    """Update logs for a task."""
    if task_id in _projects:
        _projects[task_id].logs = logs
        logger.debug(f"Task {task_id}: updated logs ({len(logs)} lines)")


def update_task_events(task_id: str, events: list[dict[str, str]]) -> None:
    """Update events for a task."""
    if task_id in _projects:
        _projects[task_id].events = events
        logger.debug(f"Task {task_id}: updated events ({len(events)} events)")


async def start_task_monitoring(task_id: str) -> None:
    """
    Start background monitoring for a task to collect logs, events, and deployment status.

    This should be called after the namespace is created and deployments are starting.
    """
    import asyncio

    if task_id not in _projects:
        logger.warning(f"Cannot start monitoring for task {task_id}: project not found")
        return

    project = _projects[task_id]
    if not project.namespace:
        logger.warning(f"Cannot start monitoring for task {task_id}: no namespace set")
        return

    logger.info(f"Starting monitoring for task {task_id} in namespace {project.namespace}")

    # Start monitoring in the background using new system
    asyncio.create_task(_monitor_project_progress(task_id))


async def _monitor_task_progress(task_id: str) -> None:
    """
    Legacy monitoring function - now delegated to new project monitoring system.
    """
    logger.debug(f"Legacy task monitoring called for {task_id} - delegating to new project monitoring")
    # Delegate to the new monitoring system
    await _monitor_project_progress(task_id)


async def monitor_argocd_deployment(task_id: str, project_name: str, progress_manager: "TaskProgressManager") -> None:
    """
    Monitor ArgoCD application synchronization and deployment.

    This polls ArgoCD to check if the application is synced and healthy.

    Args:
        task_id: The task ID for tracking
        project_name: The name of the project being deployed
        progress_manager: The progress manager for updating subtasks
    """
    import asyncio

    from opi.connectors import create_argo_connector
    from opi.connectors.kubectl import KubectlConnector

    logger.info(f"Starting ArgoCD monitoring for project {project_name}")

    # Update current step to show ArgoCD monitoring
    from opi.core.task_manager import update_progress

    update_progress(task_id, 75, f"ArgoCD applicaties zoeken voor {project_name}...")

    argo_connector = create_argo_connector()
    kubectl = KubectlConnector()

    max_wait_time = 300  # 5 minutes max for initial sync
    check_interval = 5  # Check every 5 seconds
    elapsed_time = 0

    # Check for the user-applications app first
    user_apps_synced = False
    project_apps_found = []

    while elapsed_time < max_wait_time:
        try:
            # First ensure user-applications is synced
            if not user_apps_synced:
                if await argo_connector.application_exists("user-applications"):
                    app_info = await argo_connector.get_application_status("user-applications")
                    if app_info and app_info.get("status", {}).get("sync", {}).get("status") == "Synced":
                        user_apps_synced = True
                        logger.info("User applications synced, checking for project applications")
                        update_progress(
                            task_id,
                            78,
                            f"User applications gesynchroniseerd, zoeken naar {project_name} applicaties...",
                        )

            # If user-applications is synced, look for project applications
            if user_apps_synced:
                # List all applications and find ones for this project
                all_apps = await argo_connector.list_applications()
                for app in all_apps:
                    app_name = app.get("metadata", {}).get("name", "")
                    if app_name.startswith(f"{project_name}-"):
                        if app_name not in project_apps_found:
                            project_apps_found.append(app_name)
                            logger.info(f"Found ArgoCD application: {app_name}")
                            update_progress(task_id, 80, f"ArgoCD applicatie gevonden: {app_name}")

                            # Extract namespace from the application
                            namespace = app.get("spec", {}).get("destination", {}).get("namespace")
                            if namespace and task_id in _projects:
                                # Set the correct namespace for monitoring
                                _projects[task_id].namespace = namespace
                                logger.info(f"Set monitoring namespace to: {namespace}")
                                # Start background monitoring
                                import asyncio

                                asyncio.create_task(_monitor_project_progress(task_id))

                # Check if all found applications are synced and healthy
                if project_apps_found:
                    all_synced = True
                    all_healthy = True
                    detailed_statuses = []

                    for app_name in project_apps_found:
                        app_info = await argo_connector.get_application_status(app_name)
                        if app_info:
                            sync_status = app_info.get("status", {}).get("sync", {}).get("status")
                            health_status = app_info.get("status", {}).get("health", {}).get("status")

                            # Get additional status details
                            sync_message = app_info.get("status", {}).get("sync", {}).get("message", "")
                            health_message = app_info.get("status", {}).get("health", {}).get("message", "")
                            operation_state = app_info.get("status", {}).get("operationState", {})
                            operation_phase = operation_state.get("phase", "")

                            detailed_statuses.append(
                                {
                                    "name": app_name,
                                    "sync": sync_status,
                                    "health": health_status,
                                    "sync_message": sync_message,
                                    "health_message": health_message,
                                    "operation_phase": operation_phase,
                                }
                            )

                            if sync_status != "Synced":
                                all_synced = False
                            if health_status not in ["Healthy", "Progressing"]:
                                all_healthy = False

                            logger.debug(
                                f"App {app_name}: sync={sync_status}, health={health_status}, operation={operation_phase}"
                            )

                    # Display detailed status in progress update
                    if detailed_statuses:
                        status_summary = []
                        for app_status in detailed_statuses:
                            app_short_name = app_status["name"].replace(f"{project_name}-", "")
                            sync_status = app_status["sync"]
                            health_status = app_status["health"]

                            status_text = f"{app_short_name}: sync={sync_status} health={health_status}"
                            if app_status["operation_phase"]:
                                status_text += f" ({app_status['operation_phase']})"
                            status_summary.append(status_text)

                        progress_text = " | ".join(status_summary)
                        update_progress(task_id, 81, f"ArgoCD status: {progress_text}")

                    if all_synced and all_healthy:
                        logger.info(f"All {len(project_apps_found)} ArgoCD applications are synced and healthy")
                        update_progress(task_id, 82, f"Alle {len(project_apps_found)} ArgoCD applicaties zijn gezond")

                        # Start continuous monitoring for the newly created project applications
                        asyncio.create_task(
                            _monitor_project_applications_continuously(task_id, project_name, project_apps_found)
                        )
                        break

        except Exception as e:
            logger.warning(f"Error checking ArgoCD status: {e}")

        await asyncio.sleep(check_interval)
        elapsed_time += check_interval

    if elapsed_time >= max_wait_time:
        logger.warning(f"ArgoCD monitoring timeout after {max_wait_time} seconds")

    logger.info(f"Completed initial ArgoCD monitoring for project {project_name}")


async def _monitor_project_progress(project_id: str) -> None:
    """
    Background monitoring for a project to collect logs, events, and deployment status.

    This runs while the project is in progress and updates the project data with real-time info.
    """
    import asyncio

    from opi.connectors.kubectl import KubectlConnector

    kubectl = KubectlConnector()
    monitoring_interval = 10  # seconds
    max_monitoring_time = 900  # 15 minutes max
    start_time = time.time()

    logger.debug(f"Started monitoring project {project_id}")

    try:
        while (time.time() - start_time) < max_monitoring_time:
            if project_id not in _projects:
                logger.debug(f"Project {project_id} no longer exists, stopping monitoring")
                break

            project = _projects[project_id]

            if project.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                logger.debug(f"Project {project_id} finished, stopping monitoring")
                break

            if not project.namespace:
                logger.debug(f"Project {project_id} namespace not set yet, waiting...")
                await asyncio.sleep(monitoring_interval)
                continue

            try:
                # Collect namespace events
                events = await kubectl.get_namespace_events(project.namespace, limit=20)
                if events:
                    logger.debug(f"Project {project_id}: Retrieved {len(events)} events")
                    project.events = events

                # Collect pod logs from recent deployments
                deployment_logs = []
                deployment_statuses = await kubectl.get_deployment_status(project.namespace)

                for deployment in deployment_statuses:
                    deployment_name = deployment.get("name", "")
                    if deployment_name:
                        logs = await kubectl.get_deployment_logs(deployment_name, project.namespace, lines=50)
                        if logs:
                            deployment_logs.extend(
                                [f"[{deployment_name}] {log}" for log in logs[-20:]]
                            )  # Last 20 lines per deployment

                if deployment_logs:
                    logger.debug(f"Project {project_id}: Retrieved {len(deployment_logs)} log lines")
                    project.logs = deployment_logs

            except Exception as e:
                logger.warning(f"Error collecting monitoring data for project {project_id}: {e}")

            # Wait before next monitoring cycle
            await asyncio.sleep(monitoring_interval)

    except Exception as e:
        logger.error(f"Error in monitoring project {project_id}: {e}")

    logger.debug(f"Finished monitoring project {project_id}")


async def _monitor_project_applications_continuously(
    task_id: str, project_name: str, application_names: list[str]
) -> None:
    """
    Continuously monitor project-specific ArgoCD applications after initial sync.

    This provides ongoing feedback on deployment progress, including detailed ArgoCD status
    information and pod readiness status until the deployment is fully completed.

    Args:
        task_id: The task ID for tracking
        project_name: The name of the project being deployed
        application_names: List of ArgoCD application names to monitor
    """
    import asyncio

    from opi.connectors import create_argo_connector
    from opi.connectors.kubectl import KubectlConnector

    logger.info(f"Starting continuous monitoring for project {project_name} applications: {application_names}")

    argo_connector = create_argo_connector()
    kubectl = KubectlConnector()

    monitoring_interval = 10  # Check every 10 seconds for detailed updates
    max_monitoring_time = 1800  # 30 minutes max continuous monitoring
    start_time = time.time()

    # Track deployment completion
    deployment_complete = False
    last_status_update = ""

    while (time.time() - start_time) < max_monitoring_time and not deployment_complete:
        try:
            if task_id not in _projects:
                logger.info(f"Project {task_id} no longer exists, stopping application monitoring")
                break

            project = _projects[task_id]
            if project.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                logger.info(f"Project {task_id} finished, stopping application monitoring")
                break

            # Collect detailed status for each application
            app_statuses = []
            all_healthy = True
            all_synced = True

            for app_name in application_names:
                try:
                    app_info = await argo_connector.get_application_status(app_name)
                    if app_info:
                        sync_status = app_info.get("status", {}).get("sync", {}).get("status", "Unknown")
                        health_status = app_info.get("status", {}).get("health", {}).get("status", "Unknown")

                        # Get detailed ArgoCD status information
                        sync_message = app_info.get("status", {}).get("sync", {}).get("message", "")
                        health_message = app_info.get("status", {}).get("health", {}).get("message", "")
                        operation_state = app_info.get("status", {}).get("operationState", {})
                        operation_phase = operation_state.get("phase", "")
                        operation_message = operation_state.get("message", "")

                        # Get resource status
                        resources = app_info.get("status", {}).get("resources", [])
                        resource_summary = {
                            "total": len(resources),
                            "healthy": 0,
                            "progressing": 0,
                            "degraded": 0,
                            "missing": 0,
                        }

                        for resource in resources:
                            res_health = resource.get("health", {}).get("status", "Unknown")
                            if res_health == "Healthy":
                                resource_summary["healthy"] += 1
                            elif res_health == "Progressing":
                                resource_summary["progressing"] += 1
                            elif res_health == "Degraded":
                                resource_summary["degraded"] += 1
                            else:
                                resource_summary["missing"] += 1

                        app_statuses.append(
                            {
                                "name": app_name,
                                "sync": sync_status,
                                "health": health_status,
                                "sync_message": sync_message[:100] + "..." if len(sync_message) > 100 else sync_message,
                                "health_message": health_message[:100] + "..."
                                if len(health_message) > 100
                                else health_message,
                                "operation_phase": operation_phase,
                                "operation_message": operation_message[:100] + "..."
                                if len(operation_message) > 100
                                else operation_message,
                                "resources": resource_summary,
                            }
                        )

                        if sync_status != "Synced":
                            all_synced = False
                        if health_status not in ["Healthy"]:
                            all_healthy = False

                except Exception as e:
                    logger.warning(f"Error getting status for application {app_name}: {e}")
                    app_statuses.append(
                        {
                            "name": app_name,
                            "sync": "Error",
                            "health": "Error",
                            "sync_message": str(e),
                            "health_message": "",
                            "operation_phase": "",
                            "operation_message": "",
                            "resources": {"total": 0, "healthy": 0, "progressing": 0, "degraded": 0, "missing": 0},
                        }
                    )
                    all_healthy = False
                    all_synced = False

            # Generate detailed status update
            if app_statuses:
                status_parts = []

                for app_status in app_statuses:
                    app_short_name = app_status["name"].replace(f"{project_name}-", "")

                    # Get status values
                    sync_status = app_status["sync"]
                    health_status = app_status["health"]

                    # Add resource summary if available
                    resources = app_status["resources"]
                    if resources["total"] > 0:
                        resource_text = f"({resources['healthy']}/{resources['total']} gezond"
                        if resources["progressing"] > 0:
                            resource_text += f", {resources['progressing']} bezig"
                        if resources["degraded"] > 0:
                            resource_text += f", {resources['degraded']} probleem"
                        resource_text += ")"
                    else:
                        resource_text = ""

                    # Include operation phase if available
                    operation_text = ""
                    if app_status["operation_phase"]:
                        operation_text = f" [{app_status['operation_phase']}]"

                    # Include ArgoCD messages if meaningful
                    message_text = ""
                    if app_status["health_message"] and "successfully" not in app_status["health_message"].lower():
                        message_text = f" - {app_status['health_message']}"
                    elif app_status["sync_message"] and "successfully" not in app_status["sync_message"].lower():
                        message_text = f" - {app_status['sync_message']}"
                    elif app_status["operation_message"]:
                        message_text = f" - {app_status['operation_message']}"

                    status_part = f"{app_short_name}: sync={sync_status} health={health_status}{resource_text}{operation_text}{message_text}"
                    status_parts.append(status_part)

                # Create progress update
                current_status = " | ".join(status_parts)

                # Only update if status changed significantly
                if current_status != last_status_update:
                    if all_synced and all_healthy:
                        update_progress(task_id, 85, f"Deployment voltooid: {current_status}")
                        deployment_complete = True
                    else:
                        # Calculate progress based on sync and health status
                        total_checks = len(app_statuses) * 2  # sync + health for each app
                        completed_checks = sum(1 for app in app_statuses if app["sync"] == "Synced") + sum(
                            1 for app in app_statuses if app["health"] == "Healthy"
                        )
                        progress_percent = min(82 + int((completed_checks / total_checks) * 8), 90)  # 82-90% range

                        update_progress(task_id, progress_percent, f"Deployment voortgang: {current_status}")

                    last_status_update = current_status

            # Check if deployment is complete
            if all_synced and all_healthy:
                deployment_complete = True
                logger.info(f"All applications for project {project_name} are fully deployed and healthy")
                break

        except Exception as e:
            logger.warning(f"Error during continuous application monitoring for project {project_name}: {e}")

        # Wait before next check
        await asyncio.sleep(monitoring_interval)

    if not deployment_complete:
        elapsed_minutes = (time.time() - start_time) / 60
        logger.warning(
            f"Continuous monitoring for project {project_name} ended after {elapsed_minutes:.1f} minutes without full completion"
        )
        update_progress(task_id, 84, f"Monitoring gestopt na {elapsed_minutes:.1f} min - controleer ArgoCD handmatig")

    logger.info(f"Finished continuous monitoring for project {project_name} applications")


def add_subtask(task_id: str, subtask_name: str) -> str:
    """Add a subtask to the task and return its unique ID. (Legacy function - no-op in new system)"""
    subtask_id = f"{task_id}-{subtask_name}"
    logger.debug(f"Task {task_id}: Legacy add_subtask called for '{subtask_name}' - no-op in new system")
    return subtask_id


def start_subtask(task_id: str, subtask_id: str) -> None:
    """Mark a subtask as started. (Legacy function - no-op in new system)"""
    logger.debug(f"Task {task_id}: Legacy start_subtask called for '{subtask_id}' - no-op in new system")


def complete_subtask(task_id: str, subtask_id: str) -> None:
    """Mark a subtask as completed and update overall progress. (Legacy function - no-op in new system)"""
    logger.debug(f"Task {task_id}: Legacy complete_subtask called for '{subtask_id}' - no-op in new system")


def fail_subtask(task_id: str, subtask_id: str, error: str) -> None:
    """Mark a subtask as failed. (Legacy function - no-op in new system)"""
    logger.debug(
        f"Task {task_id}: Legacy fail_subtask called for '{subtask_id}' with error: {error} - no-op in new system"
    )


def _update_task_progress_from_subtasks(task_id: str) -> None:
    """Update main task progress based on subtask completion. (Legacy function - no-op in new system)"""
    logger.debug(f"Task {task_id}: Legacy progress update from subtasks called - no-op in new system")


async def process_project_background(task_id: str, project_data: Any) -> None:
    """
    Background task function that processes a project creation request.
    Updates progress as it goes through each step.
    """

    try:
        logger.info(f"Background task {task_id} starting for project: {project_data.project_name}")

        # Mark as running
        if task_id in _projects:
            _projects[task_id].status = TaskStatus.RUNNING
            logger.debug(f"Task {task_id} marked as running")

        start_time = time.time()

        # Create progress manager for subtask tracking
        progress_manager = TaskProgressManager(task_id, project_data.project_name)

        # Add all major tasks upfront
        subtask_validate = progress_manager.add_task("Project validatie")
        subtask_yaml = progress_manager.add_task("YAML configuratie genereren")
        subtask_git_connect = progress_manager.add_task("Git repository verbinden")
        subtask_git_commit = progress_manager.add_task("Project bestand naar Git schrijven")

        # Add service-specific subtasks based on project data
        service_subtasks = {}
        if hasattr(project_data, "services") and project_data.services:
            for service in project_data.services:
                service_name = service.replace("-", " ").title()
                if "postgres" in service.lower():
                    service_subtasks["postgres"] = progress_manager.add_task("PostgreSQL database aanmaken")
                elif "keycloak" in service.lower() or "sso" in service.lower():
                    service_subtasks["keycloak"] = progress_manager.add_task("Keycloak SSO configureren")
                elif "vault" in service.lower():
                    service_subtasks["vault"] = progress_manager.add_task("Vault secrets configureren")
                elif "minio" in service.lower() or "storage" in service.lower():
                    service_subtasks["minio"] = progress_manager.add_task("MinIO storage voorbereiden")

        subtask_namespace = progress_manager.add_task("Kubernetes namespace aanmaken")
        subtask_secrets = progress_manager.add_task("SOPS secrets genereren")
        subtask_argocd = progress_manager.add_task("ArgoCD applicatie configureren")
        subtask_deploy = progress_manager.add_task("Deployment starten")
        subtask_monitor = progress_manager.add_task("ArgoCD synchronisatie monitoren")
        subtask_verify = progress_manager.add_task("Deployment verificatie")

        update_progress(task_id, 10, "Validating project data...")
        logger.info(f"Task {task_id}: Starting validation phase")

        # Validate project name (task already started automatically)
        logger.debug(f"Task {task_id}: Validating project name: {project_data.project_name}")
        from opi.utils.project_utils import validate_project_name

        if not validate_project_name(project_data.project_name):
            error_msg = f"Invalid project name format: {project_data.project_name}"
            logger.error(f"Task {task_id}: {error_msg}")
            progress_manager.fail_task(subtask_validate, error_msg)
            raise ValueError(error_msg)

        progress_manager.complete_task(subtask_validate)

        update_progress(task_id, 20, "Generating project configuration...")
        logger.info(f"Task {task_id}: Generating YAML configuration")

        # Generate YAML content from self-service form data (task already started automatically)
        try:
            from opi.utils.project_utils import generate_self_service_project_yaml

            yaml_content = await generate_self_service_project_yaml(project_data)
            logger.debug(f"Task {task_id}: Generated YAML content ({len(yaml_content)} chars)")
            progress_manager.complete_task(subtask_yaml)
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to generate YAML: {e}")
            progress_manager.fail_task(subtask_yaml, str(e))
            raise

        update_progress(task_id, 30, "Connecting to Git repository...")
        logger.info(f"Task {task_id}: Creating Git connector")

        # Create Git connector for projects repository (task already started automatically)
        try:
            git_connector_for_project_files = GitConnector(
                repo_url=settings.GIT_PROJECTS_SERVER_URL,
                username=settings.GIT_PROJECTS_SERVER_USERNAME,
                password=settings.GIT_PROJECTS_SERVER_PASSWORD,
                branch=settings.GIT_PROJECTS_SERVER_BRANCH,
                repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
            )
            logger.debug(f"Task {task_id}: Git connector created successfully")
            progress_manager.complete_task(subtask_git_connect)
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to create Git connector: {e}")
            progress_manager.fail_task(subtask_git_connect, str(e))
            raise

        update_progress(task_id, 40, "Creating project file in Git...")
        logger.info(f"Task {task_id}: Writing project file to Git")

        # Create project file in Git repository (task already started automatically)
        project_file_path = f"projects/{project_data.project_name}.yaml"
        try:
            await git_connector_for_project_files.create_or_update_file(project_file_path, yaml_content, False)
            logger.info(f"Task {task_id}: Project file created at {project_file_path}")
            progress_manager.complete_task(subtask_git_commit)
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to create project file: {e}")
            progress_manager.fail_task(subtask_git_commit, str(e))
            raise

        update_progress(task_id, 50, "Initializing project manager...")
        logger.info(f"Task {task_id}: Creating project manager")

        # Initialize component tracking based on project data
        logger.info(f"Task {task_id}: Initializing component tracking")
        if hasattr(project_data, "components") and project_data.components:
            for component_data in project_data.components:
                component_name = getattr(component_data, "name", f"component-{len(project_data.components)}")
                add_component_status(task_id, component_name)
                logger.debug(f"Task {task_id}: Added component tracking for {component_name}")
        else:
            # Default single component for simple projects
            component_name = f"{project_data.project_name}-app"
            add_component_status(task_id, component_name)
            logger.debug(f"Task {task_id}: Added default component tracking for {component_name}")

        # Note: Namespace will be set later after it's properly determined from deployment
        logger.debug(f"Task {task_id}: Namespace will be set during deployment processing")

        # Process the project file
        try:
            from opi.manager.project_manager import ProjectManager

            project_manager = ProjectManager(git_connector_for_project_files=git_connector_for_project_files)
            logger.debug(f"Task {task_id}: Project manager initialized")
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to initialize project manager: {e}")
            raise

        update_progress(task_id, 60, "Processing project deployment...")
        logger.info(f"Task {task_id}: Starting project deployment processing")

        # This is the long-running part - project processing with live progress (task already started automatically)
        try:
            processing_result = await project_manager.process_project_from_git(project_file_path, progress_manager)
            logger.info(f"Task {task_id}: Project processing completed, result: {processing_result}")
            progress_manager.complete_task(subtask_deploy)

            # ArgoCD monitoring (task already started automatically)
            update_progress(task_id, 75, "ArgoCD synchronisatie monitoren...")

            # Wait for ArgoCD to sync and start deployments
            await monitor_argocd_deployment(task_id, project_data.project_name, progress_manager)
            progress_manager.complete_task(subtask_monitor)

            # Verification (task already started automatically)
            update_progress(task_id, 85, "Verifying deployment status...")

            # Start background monitoring for logs and events
            await start_task_monitoring(task_id)

            # Wait a bit for initial deployment
            import asyncio

            await asyncio.sleep(10)

            progress_manager.complete_task(subtask_verify)

        except Exception as e:
            logger.error(f"Task {task_id}: Project processing failed: {e}")
            progress_manager.fail_task(subtask_deploy, str(e))
            raise

        elapsed_time = time.time() - start_time

        if processing_result:
            update_progress(task_id, 90, "Finalizing deployment...")

            result = {
                "project_name": project_data.project_name,
                "project_description": project_data.project_description,
                "components_count": len(project_data.components) if project_data.components else 1,
                "team_members_count": len(project_data.user_email) if project_data.user_email else 0,
                "elapsed_time": f"{elapsed_time:.2f}",
                "file_path": project_file_path,
                "status": "success",
            }

            complete_task(task_id, result)
            logger.info(
                f"Background task {task_id} completed successfully: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
            )
        else:
            # Partial success - file created but processing failed
            result = {
                "project_name": project_data.project_name,
                "file_path": project_file_path,
                "elapsed_time": f"{elapsed_time:.2f}",
                "status": "partial_success",
                "error_message": "Project file was created but processing failed. Check logs for details.",
            }

            complete_task(task_id, result)
            logger.warning(
                f"Background task {task_id} partially completed: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
            )

    except Exception as e:
        import traceback

        error_traceback = traceback.format_exc()
        error_msg = f"Error processing project: {e!s}"
        detailed_error = f"{error_msg}\n\nTraceback:\n{error_traceback}"

        fail_task(task_id, detailed_error)
        logger.error(f"Background task {task_id} failed: {error_msg}")
        logger.debug(f"Background task {task_id} full traceback:\n{error_traceback}")
