"""In-memory progress of a project run, for the progress page.

The database-backed counterpart is ``PersistentTaskProgressManager`` in
``opi/core/persistent_task_progress.py``; this one lives only in the process that runs
the work.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    name: str
    status: TaskStatus = TaskStatus.RUNNING
    parent_id: str | None = None  # If this is a subtask
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    error: str | None = None
    subject: str | None = None  # What the step runs for (deployment, component, service)


def format_step_line(name: str, subject: str | None) -> str:
    """The one-line form of a step, for the "current step" sentence.

    The step list keeps name and subject apart so the page can show them apart, but
    the running line is a single string; there the two are joined. Without this, two
    deployments doing the same step produce the same sentence twice and the line stops
    saying anything about where the run is.
    """
    return f"{name} - {subject}" if subject else name


# A step name is a LABEL, not a paragraph: the progress page puts it in the TITLE of a list
# item (progress_task_list in widgets/_macros.html.j2). The bound is a safety net against a
# caller that puts a machine dump in it, not a formatting device: every honest sentence stays
# well under it. Whoever hits it should notice, so the full message goes to the log.
MAX_STEP_NAME = 400


def clamp_step_text(text: str) -> str:
    """Shorten a step name to something that stays readable as a line title."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_STEP_NAME:
        return collapsed
    logger.warning(
        "Stapnaam van %d tekens ingekort tot %d; hoort deze tekst in component_failures? Volledig: %s",
        len(collapsed),
        MAX_STEP_NAME,
        collapsed,
    )
    return collapsed[: MAX_STEP_NAME - 1].rstrip() + "\u2026"


@dataclass
class ProjectInfo:
    id: str
    project_name: str
    status: TaskStatus
    created_at: datetime
    current_step: str = "Starting..."
    logs: list[str] | None = None
    events: list[dict[str, str]] | None = None
    namespace: str | None = None
    web_addresses: dict[str, str] | None = None  # component_name -> web_address
    completed_at: datetime | None = None  # when project reached terminal status
    error: str | None = None


_projects: dict[str, ProjectInfo] = {}
_project_managers: dict[str, TaskProgressManager] = {}
_active_monitoring_tasks: dict[str, asyncio.Task[None]] = {}
_active_app_monitoring_tasks: dict[str, asyncio.Task[None]] = {}


class TaskProgressManager:
    def __init__(self, project_id: str, project_name: str):
        self.project_id = project_id
        self.project_name = project_name
        self.tasks: dict[str, Task] = {}  # Local tasks for this project only
        # The open phase, if any: steps added while it is open become its children.
        self._phase_id: str | None = None

        _projects[project_id] = ProjectInfo(
            id=project_id,
            project_name=project_name,
            status=TaskStatus.RUNNING,
            created_at=datetime.now(tz=UTC),
        )

        _project_managers[project_id] = self
        logger.info(f"Created TaskProgressManager for project {project_name} ({project_id})")

    def add_task(self, name: str, subject: str | None = None) -> str:
        """Add a task and start it immediately. Returns task ID.

        ``subject`` says what the step runs for (a deployment, component or service name)
        for steps that run more than once per project. It is kept next to the name instead
        of inside it, so the page can show and group them separately.
        """
        task_id = str(uuid.uuid4())
        name = clamp_step_text(name)
        subject = clamp_step_text(subject) if subject else subject
        task = Task(id=task_id, name=name, status=TaskStatus.RUNNING, parent_id=self._phase_id, subject=subject)
        self.tasks[task_id] = task
        logger.info(f"Project {self.project_id}: Added task: {name} ({task_id})")
        self.update_current_step(format_step_line(name, subject))
        return task_id

    def add_subtask(self, parent_task_id: str, name: str, subject: str | None = None) -> str:
        """Add a subtask and start it immediately. Returns subtask ID.

        One level of nesting: a subtask of a step inside a phase goes under the phase.
        """
        parent = self.tasks.get(parent_task_id)
        parent_task_id = (parent.parent_id if parent else None) or parent_task_id
        subtask_id = str(uuid.uuid4())
        name = clamp_step_text(name)
        subject = clamp_step_text(subject) if subject else subject
        subtask = Task(id=subtask_id, name=name, status=TaskStatus.RUNNING, parent_id=parent_task_id, subject=subject)
        self.tasks[subtask_id] = subtask
        logger.info(f"Project {self.project_id}: Added subtask: {name} ({subtask_id}) under {parent_task_id}")
        self.update_current_step(format_step_line(name, subject))
        return subtask_id

    def begin_phase(self, name: str) -> str:
        """Close the open phase and open ``name``; see PersistentTaskProgressManager.begin_phase."""
        self._close_phase(TaskStatus.COMPLETED)
        existing = next((t for t in self.tasks.values() if t.name == name and t.parent_id is None), None)
        if existing is not None:
            existing.status = TaskStatus.RUNNING
            existing.completed_at = None
            self.update_current_step(name)
            self._phase_id = existing.id
            return existing.id
        phase_id = self.add_task(name)
        self._phase_id = phase_id
        return phase_id

    @property
    def current_phase(self) -> str | None:
        return self._phase_id

    def _close_phase(self, status: TaskStatus) -> None:
        phase = self.tasks.get(self._phase_id) if self._phase_id else None
        if phase is not None and phase.status == TaskStatus.RUNNING:
            phase.status = status
            phase.completed_at = datetime.now(tz=UTC)
        self._phase_id = None

    def update_task(self, task_id: str, message: str, subject: str | None = None) -> None:
        """Update a task's name, and its subject when one is given.

        Without ``subject`` the existing subject stays: the subject is not this method's to
        own, as test_task_step_subject records. Passing one overwrites it, which is what a
        step does that has a new reason to report on every poll.
        """
        if task_id in self.tasks:
            message = clamp_step_text(message)
            self.tasks[task_id].name = message
            if subject is not None:
                self.tasks[task_id].subject = clamp_step_text(subject)
            combined = format_step_line(message, self.tasks[task_id].subject)
            logger.info(f"Project {self.project_id}: Updated task: {combined} ({task_id})")
            self.update_current_step(combined)

    def complete_task(self, task_id: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id].status = TaskStatus.COMPLETED
            self.tasks[task_id].completed_at = datetime.now(tz=UTC)
            logger.info(f"Project {self.project_id}: Completed task: {self.tasks[task_id].name} ({task_id})")

    def fail_task(self, task_id: str, error: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id].status = TaskStatus.FAILED
            self.tasks[task_id].error = error
            self.tasks[task_id].completed_at = datetime.now(tz=UTC)
            logger.error(f"Project {self.project_id}: Failed task: {self.tasks[task_id].name} ({task_id}): {error}")

    def update_current_step(self, step: str) -> None:
        if self.project_id in _projects:
            _projects[self.project_id].current_step = step

    def complete_project(self) -> None:
        self._close_phase(TaskStatus.COMPLETED)
        if self.project_id in _projects:
            _projects[self.project_id].status = TaskStatus.COMPLETED
            _projects[self.project_id].completed_at = datetime.now(tz=UTC)

    def fail_project(self, error: str) -> None:
        self._close_phase(TaskStatus.FAILED)
        if self.project_id in _projects:
            _projects[self.project_id].status = TaskStatus.FAILED
            _projects[self.project_id].error = error
            _projects[self.project_id].completed_at = datetime.now(tz=UTC)

    def set_namespace(self, namespace: str) -> None:
        if self.project_id in _projects:
            _projects[self.project_id].namespace = namespace
            logger.info(f"Set namespace {namespace} for project {self.project_id}")
            self.start_monitoring()

    def add_logs(self, logs: list[str]) -> None:
        if self.project_id in _projects:
            _projects[self.project_id].logs = logs

    def add_events(self, events: list[dict[str, str]]) -> None:
        if self.project_id in _projects:
            _projects[self.project_id].events = events

    def start_monitoring(self) -> None:
        """Start background monitoring for logs and events. Deduplicates per project."""
        if self.project_id in _projects and _projects[self.project_id].namespace:
            _start_monitoring_if_not_active(self.project_id)
        else:
            logger.warning(f"Cannot start monitoring for project {self.project_id}: namespace not set")

    def start_subtask(self, subtask_id: str) -> None:
        start_subtask(self.project_id, subtask_id)

    def complete_subtask(self, subtask_id: str) -> None:
        complete_subtask(self.project_id, subtask_id)

    def fail_subtask(self, subtask_id: str, error: str) -> None:
        fail_subtask(self.project_id, subtask_id, error)

    def update_component_deployment(self, component_name: str, deployment_name: str) -> None:
        update_component_deployment(self.project_id, component_name, deployment_name)

    def update_component_web_address(self, component_name: str, web_address: str) -> None:
        if self.project_id in _projects:
            if _projects[self.project_id].web_addresses is None:
                _projects[self.project_id].web_addresses = {}
            _projects[self.project_id].web_addresses[component_name] = web_address
            logger.info(f"Project {self.project_id}: Updated web address for {component_name}: {web_address}")
        update_component_web_address(self.project_id, component_name, web_address)

    def update_component_readiness(self, component_name: str, deployment_ready: str) -> None:
        update_component_readiness(self.project_id, component_name, deployment_ready)


def _start_monitoring_if_not_active(project_id: str) -> None:
    """Start a monitoring task for a project only if one isn't already running."""
    existing = _active_monitoring_tasks.get(project_id)
    if existing and not existing.done():
        logger.debug(f"Monitoring already active for project {project_id}, skipping duplicate")
        return
    task = asyncio.create_task(_monitor_project_progress(project_id))
    _active_monitoring_tasks[project_id] = task
    logger.info(f"Started monitoring for project {project_id}")


def _start_app_monitoring_if_not_active(task_id: str, project_name: str, application_names: list[str]) -> None:
    """Start application monitoring for a project only if one isn't already running."""
    existing = _active_app_monitoring_tasks.get(task_id)
    if existing and not existing.done():
        logger.debug(f"Application monitoring already active for project {task_id}, skipping duplicate")
        return
    task = asyncio.create_task(_monitor_project_applications_continuously(task_id, project_name, application_names))
    _active_app_monitoring_tasks[task_id] = task
    logger.info(f"Started application monitoring for project {task_id}")


def _cleanup_completed_projects() -> None:
    """Remove projects COMPLETED or FAILED for over 30 minutes, and RUNNING ones stuck for
    over 2 hours, which are most likely orphaned.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(minutes=30)
    stuck_cutoff = datetime.now(tz=UTC) - timedelta(hours=2)
    to_remove = []
    for project_id, project in _projects.items():
        if project.status in [TaskStatus.COMPLETED, TaskStatus.FAILED] and project.completed_at:
            if project.completed_at < cutoff:
                to_remove.append(project_id)
        elif project.status == TaskStatus.RUNNING and project.created_at < stuck_cutoff:
            logger.warning(
                f"Cleaning up stuck RUNNING task {project_id} for project '{project.project_name}' "
                f"(created {project.created_at}, no completion recorded within 2 hours)"
            )
            to_remove.append(project_id)
    for project_id in to_remove:
        del _projects[project_id]
        _project_managers.pop(project_id, None)
        monitoring_task = _active_monitoring_tasks.pop(project_id, None)
        if monitoring_task and not monitoring_task.done():
            monitoring_task.cancel()
        app_monitoring_task = _active_app_monitoring_tasks.pop(project_id, None)
        if app_monitoring_task and not app_monitoring_task.done():
            app_monitoring_task.cancel()
    if to_remove:
        logger.info(f"Cleaned up {len(to_remove)} completed/failed projects from tracking")


_cleanup_task: asyncio.Task[None] | None = None


async def _periodic_cleanup_loop() -> None:
    """Periodically clean up completed/failed projects. Runs every 5 minutes."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            _cleanup_completed_projects()
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")


def start_periodic_cleanup() -> None:
    """Start the periodic cleanup background task. Safe to call multiple times."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        return
    _cleanup_task = asyncio.create_task(_periodic_cleanup_loop())
    logger.info("Started periodic project cleanup task")


def stop_periodic_cleanup() -> None:
    """Stop the periodic cleanup background task."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        logger.info("Stopped periodic project cleanup task")
    _cleanup_task = None


def create_task(project_name: str) -> str:
    """Create a new project and return its id."""
    _cleanup_completed_projects()
    project_id = str(uuid.uuid4())
    _projects[project_id] = ProjectInfo(
        id=project_id, project_name=project_name, status=TaskStatus.RUNNING, created_at=datetime.now(tz=UTC)
    )
    logger.info(f"Created project {project_id} for project {project_name}")
    return project_id


def get_task(task_id: str) -> ProjectInfo | None:
    return _projects.get(task_id)


def update_progress(task_id: str, progress: int, step: str) -> None:
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
        _projects[task_id].completed_at = datetime.now(tz=UTC)
        logger.info(f"Task {task_id} completed")
    else:
        logger.info(f"Task {task_id} completed (task not found)")


def fail_task(task_id: str, error: str) -> None:
    """Mark task as failed."""
    if task_id in _projects:
        _projects[task_id].status = TaskStatus.FAILED
        _projects[task_id].current_step = "Failed"
        _projects[task_id].completed_at = datetime.now(tz=UTC)
        logger.error(f"Task {task_id} failed: {error}")
    else:
        logger.error(f"Task {task_id} failed: {error} (task not found)")


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


async def start_task_monitoring(task_id: str) -> None:
    """Called once the namespace exists and the deployments are starting."""
    if task_id not in _projects:
        logger.warning(f"Cannot start monitoring for task {task_id}: project not found")
        return

    project = _projects[task_id]
    if not project.namespace:
        logger.warning(f"Cannot start monitoring for task {task_id}: no namespace set")
        return

    logger.info(f"Starting monitoring for task {task_id} in namespace {project.namespace}")

    _start_monitoring_if_not_active(task_id)


# Kubernetes event reasons that indicate unrecoverable deployment failures.
_UNRECOVERABLE_EVENT_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName"}


def _detect_unrecoverable_error(events: list[dict[str, str]]) -> str | None:
    """A readable message for an unrecoverable error in the namespace events, else None."""
    for event in events:
        if event.get("type") != "Warning":
            continue
        reason = event.get("reason", "")
        if reason in _UNRECOVERABLE_EVENT_REASONS:
            message = event.get("message", reason)
            obj = event.get("object", "")
            return f"{obj}: {message}" if obj else message
    return None


async def _monitor_project_progress(project_id: str) -> None:
    """Collects logs, events and deployment status while the project is in progress.

    Only one instance runs per project (enforced by _start_monitoring_if_not_active).
    """
    from opi.connectors.kubectl import KubectlConnector

    kubectl = KubectlConnector()
    monitoring_interval = 10  # seconds
    max_monitoring_time = 300  # 5 minutes max
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
                events = await kubectl.get_namespace_events(project.namespace, limit=20)
                if events:
                    logger.debug(f"Project {project_id}: Retrieved {len(events)} events")
                    project.events = events

                    unrecoverable = _detect_unrecoverable_error(events)
                    if unrecoverable:
                        logger.warning(f"Project {project_id}: Unrecoverable error detected: {unrecoverable}")
                        project.status = TaskStatus.FAILED
                        update_progress(project_id, -1, f"Deployment mislukt: {unrecoverable}")
                        break

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

            await asyncio.sleep(monitoring_interval)

    except Exception as e:
        logger.error(f"Error in monitoring project {project_id}: {e}")
    finally:
        _active_monitoring_tasks.pop(project_id, None)
        logger.debug(f"Removed monitoring tracking entry for project {project_id}")

    logger.debug(f"Finished monitoring project {project_id}")


async def _monitor_project_applications_continuously(
    task_id: str, project_name: str, application_names: list[str]
) -> None:
    """Keeps reporting on the project's ArgoCD applications after the initial sync.

    Detailed ArgoCD status and pod readiness, until the deployment is fully done. Only one
    instance runs per project (enforced by _start_app_monitoring_if_not_active).
    """
    from opi.connectors import create_argo_connector
    from opi.connectors.kubectl import KubectlConnector

    logger.info(f"Starting continuous monitoring for project {project_name} applications: {application_names}")

    argo_connector = create_argo_connector()
    kubectl = KubectlConnector()

    monitoring_interval = 10  # seconds
    max_monitoring_time = 300  # 5 minutes max
    start_time = time.time()

    deployment_complete = False
    last_status_update = ""

    try:
        while (time.time() - start_time) < max_monitoring_time and not deployment_complete:
            try:
                if task_id not in _projects:
                    logger.info(f"Project {task_id} no longer exists, stopping application monitoring")
                    break

                project = _projects[task_id]
                if project.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                    logger.info(f"Project {task_id} finished, stopping application monitoring")
                    break

                app_statuses = []
                all_healthy = True
                all_synced = True

                for app_name in application_names:
                    try:
                        app_info = await argo_connector.get_application_status(app_name)
                        if app_info:
                            sync_status = app_info.get("status", {}).get("sync", {}).get("status", "Unknown")
                            health_status = app_info.get("status", {}).get("health", {}).get("status", "Unknown")

                            sync_message = app_info.get("status", {}).get("sync", {}).get("message", "")
                            health_message = app_info.get("status", {}).get("health", {}).get("message", "")
                            operation_state = app_info.get("status", {}).get("operationState", {})
                            operation_phase = operation_state.get("phase", "")
                            operation_message = operation_state.get("message", "")

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
                                    "sync_message": sync_message[:100] + "..."
                                    if len(sync_message) > 100
                                    else sync_message,
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
                            if health_status != "Healthy":
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

                if app_statuses:
                    status_parts = []

                    for app_status in app_statuses:
                        app_short_name = app_status["name"].replace(f"{project_name}-", "")

                        sync_status = app_status["sync"]
                        health_status = app_status["health"]

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

                        operation_text = ""
                        if app_status["operation_phase"]:
                            operation_text = f" [{app_status['operation_phase']}]"

                        message_text = ""
                        if app_status["health_message"] and "successfully" not in app_status["health_message"].lower():
                            message_text = f" - {app_status['health_message']}"
                        elif app_status["sync_message"] and "successfully" not in app_status["sync_message"].lower():
                            message_text = f" - {app_status['sync_message']}"
                        elif app_status["operation_message"]:
                            message_text = f" - {app_status['operation_message']}"

                        status_part = f"{app_short_name}: sync={sync_status} health={health_status}{resource_text}{operation_text}{message_text}"
                        status_parts.append(status_part)

                    current_status = " | ".join(status_parts)

                    if current_status != last_status_update:
                        if all_synced and all_healthy:
                            update_progress(task_id, 85, f"Deployment voltooid: {current_status}")
                            deployment_complete = True
                        else:
                            total_checks = len(app_statuses) * 2  # sync + health for each app
                            completed_checks = sum(1 for app in app_statuses if app["sync"] == "Synced") + sum(
                                1 for app in app_statuses if app["health"] == "Healthy"
                            )
                            progress_percent = min(82 + int((completed_checks / total_checks) * 8), 90)  # 82-90% range

                            update_progress(task_id, progress_percent, f"Deployment voortgang: {current_status}")

                        last_status_update = current_status

                if all_synced and all_healthy:
                    deployment_complete = True
                    logger.info(f"All applications for project {project_name} are fully deployed and healthy")
                    break

            except Exception as e:
                logger.warning(f"Error during continuous application monitoring for project {project_name}: {e}")

            await asyncio.sleep(monitoring_interval)

        if not deployment_complete:
            elapsed_minutes = (time.time() - start_time) / 60
            logger.warning(
                f"Continuous monitoring for project {project_name} ended after {elapsed_minutes:.1f} minutes without full completion"
            )
            update_progress(
                task_id, 84, f"Monitoring gestopt na {elapsed_minutes:.1f} min - controleer ArgoCD handmatig"
            )

    finally:
        _active_app_monitoring_tasks.pop(task_id, None)
        logger.debug(f"Removed app monitoring tracking entry for project {task_id}")

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
