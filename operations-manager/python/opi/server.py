import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from opi.api.admin_router import admin_router
from opi.api.auth_routes import auth_router
from opi.api.backup_router import backup_router
from opi.api.federation_router import federation_router
from opi.api.image_router import image_router, push_image
from opi.api.invite_routes import invite_router
from opi.api.logs_router import logs_router
from opi.api.logs_websocket_router import logs_websocket_router
from opi.api.openapi_choices import API_DESCRIPTION, annotate_config_choices
from opi.api.prometheus_router import prometheus_router
from opi.api.resource_router import resource_router
from opi.api.restore_router import restore_router
from opi.api.router import api_router
from opi.api.step_up_router import step_up_api_router
from opi.api.task_router import task_router
from opi.api.token_router import token_router
from opi.api.v2.models import ProblemDetail, category_for_status
from opi.api.v2.router import v2_router
from opi.connectors.prometheus import close_metrics_connector
from opi.core.authz.actions import Action, action
from opi.core.config import PROJECT_DESCRIPTION, PROJECT_NAME, SECURITY_PROOF_HEADER, VERSION, settings
from opi.core.database_pools import close_database_pools

# Initialize logging first, before any other imports that might log
from opi.core.early_logging import initialize_logging  # noqa: F401 (side-effect import)
from opi.core.errors import (
    GENERIC_ERROR_TEXT,
    client_error_page,
    reference_for,
    server_error_page,
    status_phrase,
    with_reference,
)
from opi.core.logging_redact import redact_invite_key_in_path
from opi.fundament_registration import register_fundament_registration
from opi.http.auth.auth_decorators import REQUIRES_STEP_UP, requires_sso
from opi.http.auth.endpoint_util import check_route_actions
from opi.http.auth.otp_step_up import STEP_UP_403_RESPONSE, StepUpProblem
from opi.http.auth.user_token_auth import REQUIRES_PLATFORM_ADMIN_TOKEN, REQUIRES_USER_TOKEN
from opi.http.render.lotc_switch import render
from opi.http.render.static_files import CacheControlledStaticFiles, lotc_cache_control
from opi.middleware.audit import AuditMiddleware
from opi.middleware.authorization import AuthorizationMiddleware
from opi.middleware.body_limit import BodyLimitMiddleware
from opi.middleware.client_ip import PeerAddressMiddleware
from opi.middleware.openapi_etag import OpenApiETagMiddleware
from opi.middleware.rate_limit import RateLimitMiddleware
from opi.middleware.server_side_session import ServerSideSessionMiddleware
from opi.runtime.startup.startup import run_startup_tasks, warm_connector_singletons

# Importing a module registers the metric source it owns. This one is otherwise only
# reached once the backup scheduler starts, while /metrics carries its families from the
# first scrape on.
from opi.services import backup_schedule_report  # noqa: F401
from opi.services.catalog.sleep_mode.router import sleep_mode_router
from opi.services.project_store import start_reconcile_poll, stop_reconcile_poll
from opi.web.router import dev_router, web_router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from starlette.middleware.base import RequestResponseEndpoint

logger = logging.getLogger(__name__)


# todo(berry): move lifespan to own file
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Print distinctive boot banner
    from opi.runtime.startup.startup import print_boot_banner

    print_boot_banner()

    # The probe server FIRST, on its own thread: it has to answer whatever the event loop
    # is doing, so also during the rest of this startup (opi/core/probe_server.py).
    from opi.core.probe_server import start_probe_server, stop_probe_server

    start_probe_server()

    # Set up Prometheus metrics collectors
    from opi.core.metrics import setup_metrics, setup_tracemalloc, start_peak_memory_tracking

    setup_metrics()
    start_peak_memory_tracking()

    # The watchdog early: a stall during startup counts too.
    from opi.core.loop_watchdog import start_loop_watchdog, stop_loop_watchdog

    start_loop_watchdog()
    if settings.ENABLE_TRACEMALLOC:
        setup_tracemalloc()

    # Set up OpenTelemetry tracing
    from opi.core.tracing import setup_tracing

    setup_tracing(app)

    # Logging is already initialized via early_logging import
    logger.info(f"Starting {PROJECT_NAME} version {VERSION}")

    # Build the CLI connector singletons up front, or their one-time binary check lands in
    # the middle of the first task that needs them.
    await warm_connector_singletons()

    # Run startup tasks (non-fatal: failed services retry in background)
    await run_startup_tasks(app)

    # Start the project-store fallback reconcile poll: bounds how long an
    # out-of-band edit to zad-projects (member removed, invite key revoked by a
    # direct push) can go unnoticed by this instance's cache.
    start_reconcile_poll()

    # Start async task worker if enabled (combined mode)
    _worker_instance = None
    _worker_asyncio_task = None
    if settings.TASK_WORKER_ENABLED:
        try:
            from opi.core.database_pools import get_database_pool
            from opi.runtime.tasks.task_worker import TaskWorker  # type: ignore[reportMissingImports]
            from opi.services.tasks.async_task_service import (  # type: ignore[reportMissingImports]
                AsyncTaskService,
                TaskType,
            )

            get_database_pool("main")  # ensure the shared asyncpg pool is initialized
            task_service = AsyncTaskService(cluster=settings.CLUSTER_MANAGER)
            app.state.task_service = task_service

            from opi.services.oom_watcher import set_task_service

            set_task_service(task_service)

            _worker_instance = TaskWorker(task_service=task_service, cluster=settings.CLUSTER_MANAGER)

            # Imported locally to avoid circular imports; the list itself is shared with
            # the standalone worker.
            from opi.runtime.tasks.handler_registry import task_handlers

            for task_type, handler in task_handlers().items():
                _worker_instance.register_handler(task_type, handler)

            # Limit concurrent backup/restore tasks to avoid resource contention
            _worker_instance.set_type_concurrency_limit(TaskType.BACKUP, settings.BACKUP_MAX_CONCURRENT)
            _worker_instance.set_type_concurrency_limit(TaskType.RESTORE, settings.BACKUP_MAX_CONCURRENT)

            _worker_asyncio_task = asyncio.create_task(_worker_instance.run())
            logger.info("Task worker started in combined mode")

            # Start backup scheduler if enabled
            if settings.BACKUP_SCHEDULER_ENABLED:
                try:
                    from opi.runtime.backup.backup_scheduler import BackupScheduler

                    _backup_scheduler = BackupScheduler(task_service=task_service, cluster=settings.CLUSTER_MANAGER)
                    await _backup_scheduler.start()
                    app.state.backup_scheduler = _backup_scheduler
                except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
                    logger.error("Failed to start backup scheduler: %s", e)

            # Start resource tuning scheduler if enabled
            from opi.services.catalog.resource_tuning.config import resource_tuning_config

            if resource_tuning_config().scheduler_enabled:
                try:
                    from opi.runtime.periodic.resource_tuning_scheduler import ResourceTuningScheduler

                    _tuning_scheduler = ResourceTuningScheduler(cluster=settings.CLUSTER_MANAGER)
                    await _tuning_scheduler.start()
                    app.state.resource_tuning_scheduler = _tuning_scheduler
                except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
                    logger.error("Failed to start resource tuning scheduler: %s", e)

            # Audit retention has no switch of its own: see AUDIT_RETENTION_HOUR.
            try:
                from opi.runtime.periodic.audit_retention_scheduler import AuditRetentionScheduler

                _audit_retention_scheduler = AuditRetentionScheduler()
                await _audit_retention_scheduler.start()
                app.state.audit_retention_scheduler = _audit_retention_scheduler
            except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
                logger.error("Failed to start audit retention scheduler: %s", e)

            # Start reconciliation scheduler if enabled
            if settings.RECONCILIATION_SCHEDULER_ENABLED:
                try:
                    from opi.runtime.periodic.reconciliation_scheduler import ReconciliationScheduler

                    _reconciliation_scheduler = ReconciliationScheduler(cluster=settings.CLUSTER_MANAGER)
                    await _reconciliation_scheduler.start()
                    app.state.reconciliation_scheduler = _reconciliation_scheduler
                except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
                    logger.error("Failed to start reconciliation scheduler: %s", e)

        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start task worker: %s", e)
    else:
        # Frontend-only mode: still create task_service for API endpoints
        try:
            from opi.core.database_pools import get_database_pool
            from opi.services.tasks.async_task_service import AsyncTaskService  # type: ignore[reportMissingImports]

            get_database_pool("main")  # ensure the shared asyncpg pool is initialized
            task_service = AsyncTaskService(cluster=settings.CLUSTER_MANAGER)
            app.state.task_service = task_service

            from opi.services.oom_watcher import set_task_service

            set_task_service(task_service)
            logger.info("Task service initialized (worker disabled)")
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.warning(f"Failed to initialize task service: {e}")

    # Initialize federation service if configured as master
    if settings.FEDERATION_ROLE == "master" and settings.FEDERATION_PEERS:
        try:
            from opi.core.federation_config import FederationRegistry
            from opi.runtime.tasks.federation_service import FederationService

            registry = FederationRegistry.from_settings(settings.FEDERATION_PEERS, settings.CLUSTER_MANAGER)
            fed_task_service = getattr(app.state, "task_service", None)
            if fed_task_service and registry.is_enabled():
                app.state.federation_service = FederationService(registry, fed_task_service)
                logger.info("Federation service initialized (master mode, %d peers)", len(registry.get_all_peers()))
            else:
                logger.warning("Federation peers configured but task_service not available or no peers")
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error(f"Failed to initialize federation service: {e}")

    # Start the run reaper (sweeps both console and job bundles) if either is enabled.
    if settings.DB_CONSOLE_ENABLED or settings.JOB_ENABLED:
        try:
            from opi.runtime.periodic.db_console_reaper import DbConsoleReaper

            _db_console_reaper = DbConsoleReaper(cluster=settings.CLUSTER_MANAGER)
            await _db_console_reaper.start()
            app.state.db_console_reaper = _db_console_reaper
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start database console reaper: %s", e)

    # Start the log watcher scheduler if enabled
    if settings.LOGWATCHER_ENABLED:
        try:
            from opi.runtime.periodic.logwatcher_scheduler import LogwatcherScheduler

            _logwatcher_scheduler = LogwatcherScheduler()
            await _logwatcher_scheduler.start()
            app.state.logwatcher_scheduler = _logwatcher_scheduler
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start log watcher scheduler: %s", e)

    # Start the mail relay readiness watch. Without a relay there is nothing to measure,
    # and without an ntfy topic there is nowhere to report it.
    if settings.MAIL_RELAY_API_URL and settings.LOGWATCHER_NTFY_TOPIC:
        try:
            from opi.runtime.periodic.mail_relay_watcher import MailRelayWatcher

            _mail_relay_watcher = MailRelayWatcher()
            await _mail_relay_watcher.start()
            app.state.mail_relay_watcher = _mail_relay_watcher
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start mail relay watcher: %s", e)

    # Follow the routing stand of this cluster. Only with a task service: the switch queues a
    # reprocess of every project, and without a worker there is nothing to queue it on.
    _routing_task_service = getattr(app.state, "task_service", None)
    if _routing_task_service is not None:
        try:
            from opi.runtime.periodic.routing_mode_watcher import RoutingModeWatcher

            _routing_mode_watcher = RoutingModeWatcher(_routing_task_service, settings.CLUSTER_MANAGER)
            await _routing_mode_watcher.start()
            app.state.routing_mode_watcher = _routing_mode_watcher
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start routing mode watcher: %s", e)

    if settings.CAPACITY_WATCH_ENABLED and settings.CAPACITY_WATCH_TENANT:
        try:
            from opi.runtime.periodic.capacity_watcher import CapacityWatcher

            _capacity_watcher = CapacityWatcher()
            await _capacity_watcher.start()
            app.state.capacity_watcher = _capacity_watcher
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start capacity watcher: %s", e)

    # Start the cluster-wide OOM pod watch if this cluster runs it
    from opi.core.cluster_config import watches_pods_for_oom

    if watches_pods_for_oom(settings.CLUSTER_MANAGER):
        try:
            from opi.services.oom_watcher import OomMetricSweeper, OomPodWatcher

            _oom_pod_watcher = OomPodWatcher(cluster=settings.CLUSTER_MANAGER)
            await _oom_pod_watcher.start()
            app.state.oom_pod_watcher = _oom_pod_watcher

            _oom_metric_sweeper = OomMetricSweeper(
                cluster=settings.CLUSTER_MANAGER,
                interval_seconds=settings.OOM_METRIC_SWEEP_INTERVAL_SECONDS,
            )
            await _oom_metric_sweeper.start()
            app.state.oom_metric_sweeper = _oom_metric_sweeper
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start the OOM pod watch: %s", e)

    # Count the pods that cannot pull their image, on every cluster. Not behind a switch
    # like the watch above; the reason is on ``ImagePullObserver``.
    try:
        from opi.services.image_pull_report import ImagePullObserver

        _image_pull_observer = ImagePullObserver(interval_seconds=settings.IMAGE_PULL_OBSERVE_INTERVAL_SECONDS)
        await _image_pull_observer.start()
        app.state.image_pull_observer = _image_pull_observer
    except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
        logger.error("Failed to start the image-pull observer: %s", e)

    # Start the sleep-mode sweeper if enabled
    if settings.SLEEP_MODE_SCHEDULER_ENABLED:
        try:
            from opi.services.catalog.sleep_mode.scheduler import SleepModeScheduler

            _sleep_mode_scheduler = SleepModeScheduler(cluster=settings.CLUSTER_MANAGER)
            await _sleep_mode_scheduler.start()
            app.state.sleep_mode_scheduler = _sleep_mode_scheduler
        except Exception as e:  # noqa: BLE001  # the app lifecycle must not stop on one component
            logger.error("Failed to start sleep-mode sweeper: %s", e)

    yield

    # Begin graceful drain: reject new task creation via API immediately
    from opi.core.shutdown import begin_drain

    begin_drain()

    # The probe server goes down LAST, further below: the kubelet has to keep getting an
    # answer for as long as we are shutting down.

    # Stop backup scheduler
    backup_scheduler = getattr(app.state, "backup_scheduler", None)
    if backup_scheduler is not None:
        await backup_scheduler.stop()

    # Stop resource tuning scheduler
    resource_tuning_scheduler = getattr(app.state, "resource_tuning_scheduler", None)
    if resource_tuning_scheduler is not None:
        await resource_tuning_scheduler.stop()

    # Stop audit retention scheduler
    audit_retention_scheduler = getattr(app.state, "audit_retention_scheduler", None)
    if audit_retention_scheduler is not None:
        await audit_retention_scheduler.stop()

    # Stop reconciliation scheduler
    reconciliation_scheduler = getattr(app.state, "reconciliation_scheduler", None)
    if reconciliation_scheduler is not None:
        await reconciliation_scheduler.stop()

    # Stop database console reaper
    db_console_reaper = getattr(app.state, "db_console_reaper", None)
    if db_console_reaper is not None:
        await db_console_reaper.stop()

    # Stop the OOM pod watch
    oom_metric_sweeper = getattr(app.state, "oom_metric_sweeper", None)
    if oom_metric_sweeper is not None:
        await oom_metric_sweeper.stop()

    oom_pod_watcher = getattr(app.state, "oom_pod_watcher", None)
    if oom_pod_watcher is not None:
        await oom_pod_watcher.stop()

    # Stop the image-pull observer
    image_pull_observer = getattr(app.state, "image_pull_observer", None)
    if image_pull_observer is not None:
        await image_pull_observer.stop()

    # Stop sleep-mode sweeper
    sleep_mode_scheduler = getattr(app.state, "sleep_mode_scheduler", None)
    if sleep_mode_scheduler is not None:
        await sleep_mode_scheduler.stop()

    # Stop log watcher scheduler
    logwatcher_scheduler = getattr(app.state, "logwatcher_scheduler", None)
    if logwatcher_scheduler is not None:
        await logwatcher_scheduler.stop()

    mail_relay_watcher = getattr(app.state, "mail_relay_watcher", None)
    if mail_relay_watcher is not None:
        await mail_relay_watcher.stop()

    routing_mode_watcher = getattr(app.state, "routing_mode_watcher", None)
    if routing_mode_watcher is not None:
        await routing_mode_watcher.stop()

    capacity_watcher = getattr(app.state, "capacity_watcher", None)
    if capacity_watcher is not None:
        await capacity_watcher.stop()

    # Stop the metrics connector: its recovery task and its HTTP client
    await close_metrics_connector()

    # Stop task worker: stop claiming new tasks, then wait for active tasks to finish
    if _worker_instance is not None:
        await _worker_instance.stop()
    # Cancel the worker asyncio task to clean up helper loops (stale recovery, cleanup)
    if _worker_asyncio_task is not None:
        _worker_asyncio_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _worker_asyncio_task
        logger.info("Task worker stopped")

    # Stop peak memory tracking
    from opi.core.metrics import stop_peak_memory_tracking

    stop_peak_memory_tracking()

    # Stop the project-store fallback reconcile poll
    stop_reconcile_poll()

    # Close database connection pools
    try:
        await close_database_pools()
        logger.info("Database pools closed successfully")
    except Exception as e:  # noqa: BLE001  # shutdown continues past a failing close
        logger.error(f"Error closing database pools: {e}")

    # Dispose the ORM async engine (service-owned persistence, opi.core.db). Guarded by a
    # timeout so a stuck connection can never hang the shutdown -- the pod must always be
    # able to terminate.
    try:
        from opi.core.db import dispose_engine

        await asyncio.wait_for(dispose_engine(), timeout=10.0)
        logger.info("ORM async engine disposed successfully")
    except TimeoutError:
        logger.error("Timed out disposing ORM async engine after 10s; continuing shutdown")
    except Exception as e:  # noqa: BLE001  # shutdown continues past a failing close
        logger.error(f"Error disposing ORM async engine: {e}")

    # The shared HTTP sessions to ArgoCD: an open connector would make closing the loop
    # emit a warning.
    try:
        from opi.connectors.argo import close_shared_sessions

        await close_shared_sessions()
    except Exception as e:  # noqa: BLE001  # shutdown continues past a failing close
        logger.error(f"Error closing shared ArgoCD sessions: {e}")

    stop_loop_watchdog()

    # Shut down OpenTelemetry tracing (flush pending spans)
    from opi.core.tracing import shutdown_tracing

    shutdown_tracing()

    # Very last: up to here the kubelet could still get an answer.
    stop_probe_server()

    logger.info(f"Stopping application {PROJECT_NAME} version {VERSION}")
    logging.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="RIG Operations Manager API",
        description=API_DESCRIPTION,
        summary=PROJECT_DESCRIPTION,
        version=VERSION,
        # Swagger and ReDoc are not here but as routes of their own with requires_sso, below.
        docs_url=None,
        redoc_url=None,
        # Guarding this does not protect anything (zad-cli reads it live) and breaks the CLI.
        openapi_url="/openapi.json",
        default_response_class=HTMLResponse,
        debug=settings.DEBUG,
        contact={
            "name": "RIG Operations Team",
            "url": "https://github.com/your-org/rig-cluster",
        },
        license_info={
            "name": "Internal Use",
        },
    )

    # Add custom OpenAPI schema with security definitions
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        from fastapi.openapi.utils import get_openapi

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        # Two ways to identify yourself, because there are two kinds of endpoint.
        openapi_schema["components"]["securitySchemes"] = {
            "APIKeyHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "API key for project authentication",
            },
            "BearerToken": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "SSO access token, for the endpoints that cannot use a project's API key "
                    "because they are not about one project yet: listing the projects you may "
                    "see, creating a project, and platform maintenance by a platform admin. "
                    "Send it as 'Authorization: Bearer <token>'."
                ),
            },
        }

        # Which route wants which scheme, read from the route itself. It must not be
        # APIKeyHeader on every /api/ operation: the two that cannot use a project key would
        # then make a client send X-API-Key and get an unexplainable 401.
        bearer_operations = {
            (route.path, method.lower())
            for route in app.routes
            if getattr(route, "endpoint", None) is not None
            and any(
                getattr(route.endpoint, marker, False)
                for marker in (REQUIRES_USER_TOKEN, REQUIRES_PLATFORM_ADMIN_TOKEN)
            )
            for method in getattr(route, "methods", None) or []
        }

        # The error envelope, once as a schema and then on every /api/ operation. ``5XX``
        # is the range form OpenAPI has for that.
        schemas = openapi_schema["components"].setdefault("schemas", {})
        envelope = ProblemDetail.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(envelope.pop("$defs", {}))
        schemas["ProblemDetail"] = envelope

        # The routes that ask for step-up: @requires_step_up or
        # @requires_platform_admin_token. Clients read the 403 from this document.
        step_up_operations = {
            (route.path, method.lower())
            for route in app.routes
            if getattr(route, "endpoint", None) is not None
            and any(
                getattr(route.endpoint, marker, None) for marker in (REQUIRES_STEP_UP, REQUIRES_PLATFORM_ADMIN_TOKEN)
            )
            for method in getattr(route, "methods", None) or []
        }
        step_up_schema = StepUpProblem.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(step_up_schema.pop("$defs", {}))
        schemas["StepUpProblem"] = step_up_schema
        five_xx = {
            "description": (
                "Er ging iets mis aan onze kant. Het lag niet aan je verzoek, dus opnieuw "
                "proberen kan helpen; 'reference' is het kenmerk waarmee een beheerder deze "
                "fout in de log terugvindt."
            ),
            "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDetail"}}},
        }

        for path, methods in openapi_schema["paths"].items():
            if not path.startswith("/api/"):
                continue
            for verb, method in methods.items():
                if not isinstance(method, dict) or "operationId" not in method:
                    continue
                scheme = "BearerToken" if (path, verb) in bearer_operations else "APIKeyHeader"
                method["security"] = [{scheme: []}]
                method.setdefault("responses", {})["5XX"] = five_xx
                if (path, verb) in step_up_operations:
                    method["responses"]["403"] = STEP_UP_403_RESPONSE

        # Sort paths: V2 first, then v1, for clarity in docs
        paths = openapi_schema.get("paths", {})
        sorted_paths = dict(sorted(paths.items(), key=lambda p: (not p[0].startswith("/api/v2"), p[0])))
        openapi_schema["paths"] = sorted_paths

        # Which build made this document. info.version is the API's number and does not
        # move with a change to the spec, so a client cannot tell from it whether it still
        # looks at the same truth. This is readable without a second request; the ETag in
        # opi/middleware/openapi_etag.py is what makes a conditional GET possible.
        from opi.core.version import get_version_info

        version_info = get_version_info()
        openapi_schema["info"]["x-spec-revision"] = {
            key: version_info.get(key, "") for key in ("commit", "branch", "build_date")
        }

        # Add API version info
        openapi_schema["info"]["x-api-info"] = {
            "v1_status": "deprecated - use /api/v2 endpoints",
            "v2_status": "current - recommended",
            "choices": (
                "Config fields that offer a fixed set of values carry `x-choices` "
                "(`{const, title}` per value); fields whose values depend on the project "
                "carry `x-choices-source`, naming the endpoint that serves the list."
            ),
        }

        # The allowed values per config field, derived from the same declaration the form
        # uses. Not in the models: a list of choices can differ per cluster, and this
        # document is served by that cluster.
        annotate_config_choices(openapi_schema)

        app.openapi_schema = openapi_schema
        return app.openapi_schema

    app.openapi = custom_openapi

    # Add middleware in the correct order (reverse order of execution)
    # ProxyHeadersMiddleware must be last (runs first) to set correct scheme from X-Forwarded-Proto
    from opi.middleware.maintenance import MaintenanceMiddleware
    from opi.utils.csrf import CSRFMiddleware

    def _wants_a_page(request: Request) -> bool:
        """Whether this caller wants HTML, or the JSON every client parses today.

        The ``Accept`` header decides, not the path, with one exception: nothing under
        ``/api`` may ever return markup, not even to a browser.
        """
        if request.url.path.startswith("/api"):
            return False
        return "text/html" in request.headers.get("accept", "")

    def _error_response(
        request: Request, status_code: int, detail: object, headers: Mapping[str, str] | None = None
    ) -> Response:
        """The answer to a 5xx: a page for a browser, an envelope for everything else.

        The exception itself must NOT end up in here: it names internal hosts and ports, and
        belongs in the log only. The reference is the link between the two: the same
        characters on the screen and on every log line of this request.

        The exception's headers travel along, so a promise like the ``Retry-After`` of the
        upload slot reaches the client. Only the headers about the body itself do not,
        because that body is rebuilt here.
        """
        text = detail if isinstance(detail, str) and detail.strip() else GENERIC_ERROR_TEXT
        if text == status_phrase(status_code):
            # FastAPI's default text ("Internal Server Error") tells the reader nothing
            # about what to do, so replace it with the sentence that does.
            text = GENERIC_ERROR_TEXT
        reference = reference_for(request)
        pass_through = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower() not in ("content-length", "content-type")
        } or None
        if _wants_a_page(request):
            return HTMLResponse(server_error_page(text, reference), status_code=status_code, headers=pass_through)
        problem = ProblemDetail(
            title=status_phrase(status_code),
            status=status_code,
            detail=with_reference(text, reference),
            instance=request.url.path,
            category=category_for_status(status_code),
            reference=reference or None,
        )
        return JSONResponse(
            content=problem.model_dump(),
            status_code=status_code,
            media_type="application/problem+json",
            headers=pass_through,
        )

    # Log all unhandled exceptions through Python logging so they appear in
    # kubectl logs.  Starlette's ServerErrorMiddleware only prints to stderr
    # via traceback.print_exc() which may not reach the log stream.
    @app.exception_handler(Exception)
    async def _log_unhandled_exceptions(request: Request, exc: Exception) -> Response:
        """Log the exception and answer with something that helps a human further.

        Starlette re-raises after this response, so the server keeps seeing the error.
        """
        logger.exception("Unhandled exception on %s %s", request.method, redact_invite_key_in_path(request.url.path))
        return _error_response(request, 500, None)

    @app.exception_handler(StarletteHTTPException)
    async def _error_page(request: Request, exc: StarletteHTTPException) -> Response:
        """Serve an error as a page to a browser and as JSON to everything else.

        The client says which one it wants, so read it: an /api path or a caller that does
        not ask for HTML keeps the JSON body every client parses today. A 5xx runs through
        the same choice, with an envelope instead of a bare ``detail`` field.
        """
        if exc.status_code >= 500:
            return _error_response(request, exc.status_code, exc.detail, exc.headers)
        if not _wants_a_page(request):
            return await http_exception_handler(request, exc)
        return HTMLResponse(
            client_error_page(exc.status_code, exc.detail), status_code=exc.status_code, headers=exc.headers
        )

    from opi.middleware.security_headers import SecurityHeadersMiddleware

    # Added first, so executed innermost: the document is only answered after maintenance
    # and authorization have let the request through.
    #
    # The audit sits inside that again, directly around the router, and has to: the commit
    # SHAs and the task id of this request travel as contextvars, and a BaseHTTPMiddleware
    # (CSRF, authorization, maintenance) runs the rest of the application in a task of its
    # own, so a contextvar set in there does not come back out.
    app.add_middleware(AuditMiddleware)
    app.add_middleware(OpenApiETagMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(AuthorizationMiddleware)
    app.add_middleware(MaintenanceMiddleware)
    # Harden the session cookie. same_site stays "lax" (not "strict") on
    # purpose: the OIDC login flow returns to /auth/callback via a top-level
    # cross-site GET redirect from Keycloak, and authlib needs its state cookie
    # to survive that navigation. "strict" would drop the cookie on the
    # callback and break login. "lax" still blocks cross-site POST/AJAX, and
    # CSRFMiddleware (double-submit token + Origin/Referer check) is the real
    # defense against the same-site sibling-subdomain tenant attacker.
    app.add_middleware(
        ServerSideSessionMiddleware,
        secret_key=settings.SECRET_KEY,
        same_site="lax",
        https_only=not settings.DEBUG,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
    )
    # Inside the security headers (a 429 carries them too) and outside session, maintenance
    # and authorization: a flood is refused before it costs those steps.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware,
        keycloak_url=settings.KEYCLOAK_URL,
        prometheus_url=settings.PROMETHEUS_EXTERNAL_URL or settings.PROMETHEUS_URL,
    )
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])  # type: ignore[arg-type]
    # Outside ProxyHeadersMiddleware, so executed before it: that one overwrites
    # scope["client"] with a value from X-Forwarded-For, after which the peer address can no
    # longer be read. See opi/utils/client_ip.py.
    app.add_middleware(PeerAddressMiddleware)
    # Outside session, CSRF and authorization: an oversized attachment upload is refused
    # before the multipart parser receives its body.
    app.add_middleware(BodyLimitMiddleware, image_push_endpoint=push_image)

    # Flow ID middleware - runs first (outermost) to tag all log lines for this request
    from opi.core.flow_id import set_flow_id

    @app.middleware("http")
    async def flow_id_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # In the scope as well as the contextvar: the handler for an unhandled exception
        # runs outside this middleware, where a contextvar no longer reaches.
        request.state.flow_id = set_flow_id("req")
        return await call_next(request)

    # Initialize OAuth client (registration happens during startup after Keycloak setup)
    oauth = OAuth()
    app.state.oauth = oauth
    logger.info("OAuth client initialized - registration will happen after Keycloak setup")

    # Include routers - only API routers will appear in OpenAPI docs
    app.include_router(auth_router, include_in_schema=False)  # Exclude from OpenAPI docs
    app.include_router(api_router, include_in_schema=True)  # Include in OpenAPI docs
    app.include_router(backup_router, include_in_schema=True)  # Include in OpenAPI docs
    app.include_router(restore_router, include_in_schema=True)  # Include in OpenAPI docs
    app.include_router(image_router, include_in_schema=True)  # Image upload proxy
    app.include_router(logs_router, include_in_schema=True)  # Include in OpenAPI docs
    app.include_router(logs_websocket_router, include_in_schema=False)  # WebSocket for log streaming
    app.include_router(resource_router, include_in_schema=True)  # Resource tuning & sanitization
    app.include_router(sleep_mode_router, include_in_schema=True)  # Sleep-mode wake/status (waker pod + project key)
    app.include_router(v2_router, include_in_schema=True)  # V2 async API endpoints
    app.include_router(task_router, include_in_schema=True)  # Async task status API
    app.include_router(token_router, include_in_schema=True)  # API tokens and service accounts
    app.include_router(federation_router, include_in_schema=True)  # Federation peers/health
    app.include_router(admin_router, include_in_schema=True)  # Admin cleanup/reconciliation API
    app.include_router(step_up_api_router, include_in_schema=True)  # Step-up state of a bearer token
    app.include_router(prometheus_router, include_in_schema=False)  # Prometheus /metrics scrape endpoint
    app.include_router(invite_router, include_in_schema=False)  # Exclude from OpenAPI docs (public invite flow)
    app.include_router(web_router, include_in_schema=False)  # Exclude from OpenAPI docs
    register_fundament_registration(app)  # Fieldlab registration; only where its code is set

    # The assets of the component system are spread over several installed packages (the
    # core plus each design system), hence a route with several roots instead of a mount.
    from opi.http.render.templates_lotc import resolve_lotc_static
    from opi.web.lotc_router import router as lotc_web_router

    # The demo setup and test pages show fixture data without SSO, so local and sandbox
    # only. Without ENVIRONMENT the value is "production" and they do not exist.
    if settings.is_development_environment:
        app.include_router(lotc_web_router, include_in_schema=False)
        app.include_router(dev_router, include_in_schema=False)
        logger.info("LOTC-proefopstelling en testpagina's geregistreerd (ENVIRONMENT=%s)", settings.ENVIRONMENT)

    @app.get("/static/lotc/{rel:path}", include_in_schema=False)
    async def lotc_static(rel: str, request: Request) -> FileResponse:
        resolved = resolve_lotc_static(rel)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Static asset not found")
        # Without Cache-Control the browser guesses how long its copy stays fresh, and
        # after an upgrade it runs the old nldd.js against new markup for days.
        cache_control = lotc_cache_control(rel, request.scope["query_string"])
        return FileResponse(resolved, headers={"cache-control": cache_control})

    logger.info("LOTC static files served at /static/lotc/")

    # Mount regular static files last (more general path)
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.exists(static_dir):
        app.mount("/static", CacheControlledStaticFiles(directory=static_dir), name="static")
        logger.info(f"Regular static files mounted at /static from {static_dir}")

    # Favicon at the root path (browsers request /favicon.ico automatically)
    favicon_path = os.path.join(static_dir, "favicon.ico")

    @app.get("/favicon.ico", include_in_schema=False, response_class=FileResponse)
    async def favicon() -> FileResponse:
        """Serve favicon from the expected root path."""
        return FileResponse(favicon_path, media_type="image/x-icon")

    # The API documentation is admin tooling, not a public page: FastAPI's own paths are
    # off (docs_url/redoc_url) and these two ask for a session. Templates of our own instead
    # of get_swagger_ui_html/get_redoc_html, because those build their scripts in Python and
    # cannot carry a nonce (features/security-headers.md). The assets come from static/
    # instead of cdn.jsdelivr.net (see static/HERKOMST.md).
    @app.get("/docs", include_in_schema=False)
    @action(Action.PORTAL_READ)
    @requires_sso
    async def swagger_ui(request: Request) -> Response:
        return render(
            request,
            template="api-docs-swagger.html.j2",
            context={"titel": f"{PROJECT_NAME} - Swagger UI", "openapi_url": "/openapi.json"},
        )

    @app.get("/redoc", include_in_schema=False)
    @action(Action.PORTAL_READ)
    @requires_sso
    async def redoc_ui(request: Request) -> Response:
        return render(
            request,
            template="api-docs-redoc.html.j2",
            context={"titel": f"{PROJECT_NAME} - ReDoc", "openapi_url": "/openapi.json"},
        )

    # security.txt: redirect to NCSC central file per Rijksoverheid guidance
    # https://www.ncsc.nl/.well-known/security.txt
    @app.get("/.well-known/security.txt", include_in_schema=False)
    async def security_txt() -> RedirectResponse:
        return RedirectResponse(
            url="https://www.ncsc.nl/.well-known/security.txt",
            status_code=302,
        )

    # Liveness probe - always OK (keeps the pod alive)
    @app.get("/health", include_in_schema=False, response_class=JSONResponse)
    @app.get("/healthz", include_in_schema=False, response_class=JSONResponse)
    async def liveness_check() -> JSONResponse:
        """Liveness probe for Kubernetes; carries the proof-target token back when it is set."""
        token = settings.SECURITY_PROOF_TOKEN
        headers = {SECURITY_PROOF_HEADER: token} if token else None
        return JSONResponse({"status": "ok"}, headers=headers)

    # Version info - public, so anyone (and the E2E suite) can see which build is running.
    @app.get("/version", include_in_schema=True, tags=["meta"], response_class=JSONResponse, response_model=None)
    async def version_info() -> dict[str, object]:
        """Return the running build's version metadata (version, commit, branch, ...)."""
        from opi.core.version import get_version_info

        return get_version_info()

    # Readiness probe - only OK when all services are up
    @app.get("/readyz", include_in_schema=False, response_class=JSONResponse, response_model=None)
    async def readiness_check() -> dict[str, str] | JSONResponse:
        """Readiness probe for Kubernetes - returns OK only when all services are ready."""
        from opi.core.readiness import get_readiness_state

        readiness = get_readiness_state()
        if readiness.is_ready:
            return {"status": "ok"}
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "services": readiness.summary()},
        )

    # A guarded route without an action would quietly open everything the caller was already
    # allowed, and an action without a rule opens nothing without anyone noticing. Here and
    # not in the startup tasks: the route table is finished as soon as the app is composed.
    check_route_actions(app.routes)

    return app


app = create_app()


# Conditional startup for different development modes
if __name__ == "__main__":
    import uvicorn

    from opi.core.config import settings

    if settings.DEBUG_MODE == "debug":
        # Debug mode: Start with debugpy, no reload
        import debugpy

        # Localhost only: kubectl port-forward talks to localhost in the pod, so the
        # Skaffold debug route works without the port hanging on the cluster network.
        debugpy.listen(("127.0.0.1", 5678))
        logger.info("🐛 Debug mode: Waiting for debugger to attach on port 5678...")
        debugpy.wait_for_client()
        logger.info("🐛 Debugger attached! Starting server...")
        uvicorn.run("opi.server:app", host="0.0.0.0", port=8000, reload=False)
    elif settings.DEBUG_MODE == "reload":
        # Reload mode: Fast iteration, no debugging
        logger.info("🔥 Hot-reload mode: File changes will auto-reload (debounce: 2.5s)")
        uvicorn.run(
            "opi.server:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            reload_delay=2.5,  # Wait 2.5s for file changes to settle before reloading
            reload_dirs=["/app/opi", "/app/templates", "/app/manifests"],
        )
    else:
        # Production mode: No reload, no debugging
        # Use asyncio event loop instead of uvloop to avoid zombie process accumulation
        # when running as PID 1 in a container (uvloop's libuv-based child reaping is
        # unreliable without a proper init process)
        uvicorn.run(app, host="0.0.0.0", port=8000, loop="asyncio")
