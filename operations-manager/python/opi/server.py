import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import FastAPI, HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from opi.api.admin_router import admin_router
from opi.api.auth_routes import auth_router
from opi.api.backup_router import backup_router
from opi.api.federation_router import federation_router
from opi.api.image_router import image_router
from opi.api.invite_routes import invite_router
from opi.api.logs_router import logs_router
from opi.api.logs_websocket_router import logs_websocket_router
from opi.api.prometheus_router import prometheus_router
from opi.api.resource_router import resource_router
from opi.api.restore_router import restore_router
from opi.api.router import api_router
from opi.api.task_router import task_router
from opi.api.v2.router import v2_router
from opi.core.config import PROJECT_DESCRIPTION, PROJECT_NAME, VERSION, settings
from opi.core.database_pools import close_database_pools

# Initialize logging first, before any other imports that might log
from opi.core.early_logging import initialize_logging  # noqa: F401 (side-effect import)
from opi.core.git_monitor import start_git_monitoring, stop_git_monitoring
from opi.core.startup import run_startup_tasks
from opi.core.static_files import CacheControlledStaticFiles
from opi.core.task_manager import start_periodic_cleanup, stop_periodic_cleanup
from opi.middleware.authorization import AuthorizationMiddleware
from opi.services.catalog.sleep_mode.router import sleep_mode_router
from opi.services.project_store import start_reconcile_poll, stop_reconcile_poll
from opi.web.router import web_router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

#: De 404-pagina. Bewust zelfstandig: hij moet ook renderen als het thema, de
#: sjablonenmap of de sessie juist het probleem is.
_NOT_FOUND_PAGE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pagina niet gevonden - ZAD</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; display: grid; place-items: center;
         min-height: 100vh; color: #154273; background: #fff; }
  main { text-align: center; padding: 2rem; }
  h1 { font-size: 2rem; margin: 0 0 .5rem; }
  p { color: #4a4a4a; margin: 0 0 1.5rem; }
  a { color: #154273; }
</style>
</head>
<body>
<main>
  <h1>Deze pagina bestaat niet</h1>
  <p>De link klopt niet meer, of de pagina is verplaatst.</p>
  <a href="/dashboard">Naar het dashboard</a>
</main>
</body>
</html>
"""


# todo(berry): move lifespan to own file
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Print distinctive boot banner
    from opi.core.startup import print_boot_banner

    print_boot_banner()

    # Set up Prometheus metrics collectors
    from opi.core.metrics import setup_metrics, setup_tracemalloc, start_peak_memory_tracking

    setup_metrics()
    start_peak_memory_tracking()
    if settings.ENABLE_TRACEMALLOC:
        setup_tracemalloc()

    # Set up OpenTelemetry tracing
    from opi.core.tracing import setup_tracing

    setup_tracing(app)

    # Logging is already initialized via early_logging import
    logger.info(f"Starting {PROJECT_NAME} version {VERSION}")
    # logger.info(f"Settings: {mask.secrets(get_settings().model_dump())}")

    # Run startup tasks (non-fatal: failed services retry in background)
    await run_startup_tasks(app)

    # Start Git monitoring service
    if settings.ENABLE_GIT_MONITOR:
        try:
            await start_git_monitoring(app)
            logger.info("Git monitoring service started successfully")
        except Exception as e:
            logger.error(f"Failed to start Git monitoring service: {e}")

    # Start periodic task cleanup
    start_periodic_cleanup()

    # Start the project-store fallback reconcile poll: bounds how long an
    # out-of-band edit to zad-projects (member removed, invite key revoked by a
    # direct push) can go unnoticed by this instance's cache.
    start_reconcile_poll()

    # Start async task worker if enabled (combined mode)
    _worker_instance = None
    _worker_asyncio_task = None
    if settings.TASK_WORKER_ENABLED:
        try:
            from opi.core.async_task_service import AsyncTaskService, TaskType  # type: ignore[reportMissingImports]
            from opi.core.database_pools import get_database_pool
            from opi.core.task_worker import TaskWorker  # type: ignore[reportMissingImports]

            get_database_pool("main")  # ensure the shared asyncpg pool is initialized
            task_service = AsyncTaskService(cluster=settings.CLUSTER_MANAGER)
            app.state.task_service = task_service

            from opi.services.oom_watcher import set_task_service

            set_task_service(task_service)

            _worker_instance = TaskWorker(task_service=task_service, cluster=settings.CLUSTER_MANAGER)

            # Register handlers (imported locally to avoid circular imports)
            from opi.core.task_handlers_backup import (  # type: ignore[reportMissingImports]
                handle_backup,
                handle_restore,
            )
            from opi.core.task_handlers_components import (  # type: ignore[reportMissingImports]
                handle_add_component,
                handle_add_component_to_deployment,
                handle_add_service,
                handle_configure_service,
                handle_configure_service_values,
                handle_delete_component,
                handle_manage_database_schemas,
                handle_update_component,
            )
            from opi.core.task_handlers_deployment import (  # type: ignore[reportMissingImports]
                handle_delete_deployment,
                handle_update_image,
            )
            from opi.core.task_handlers_operations import (  # type: ignore[reportMissingImports]
                handle_clone_bucket,
                handle_clone_database,
                handle_refresh_deployment,
                handle_refresh_project,
            )
            from opi.core.task_handlers_project import (  # type: ignore[reportMissingImports]
                handle_create_project,
                handle_delete_project,
                handle_upsert_deployment,
            )
            from opi.services.catalog.attachments.task import handle_delete_attachment
            from opi.services.catalog.sleep_mode.task import handle_sleep_transition

            _worker_instance.register_handler(TaskType.CREATE_PROJECT, handle_create_project)
            _worker_instance.register_handler(TaskType.UPSERT_DEPLOYMENT, handle_upsert_deployment)
            _worker_instance.register_handler(TaskType.UPDATE_IMAGE, handle_update_image)
            _worker_instance.register_handler(TaskType.DELETE_DEPLOYMENT, handle_delete_deployment)
            _worker_instance.register_handler(TaskType.DELETE_PROJECT, handle_delete_project)
            _worker_instance.register_handler(TaskType.DELETE_COMPONENT, handle_delete_component)
            _worker_instance.register_handler(TaskType.DELETE_ATTACHMENT, handle_delete_attachment)
            _worker_instance.register_handler(TaskType.CLONE_DATABASE, handle_clone_database)
            _worker_instance.register_handler(TaskType.CLONE_BUCKET, handle_clone_bucket)
            _worker_instance.register_handler(TaskType.REFRESH_DEPLOYMENT, handle_refresh_deployment)
            _worker_instance.register_handler(TaskType.SLEEP_DEPLOYMENT, handle_sleep_transition)
            _worker_instance.register_handler(TaskType.WAKE_DEPLOYMENT, handle_sleep_transition)
            _worker_instance.register_handler(TaskType.REFRESH_PROJECT, handle_refresh_project)
            _worker_instance.register_handler(TaskType.ADD_COMPONENT, handle_add_component)
            _worker_instance.register_handler(TaskType.UPDATE_COMPONENT, handle_update_component)
            _worker_instance.register_handler(TaskType.ADD_COMPONENT_TO_DEPLOYMENT, handle_add_component_to_deployment)
            _worker_instance.register_handler(TaskType.ADD_SERVICE, handle_add_service)
            _worker_instance.register_handler(TaskType.CONFIGURE_SERVICE, handle_configure_service)
            _worker_instance.register_handler(TaskType.CONFIGURE_SERVICE_VALUES, handle_configure_service_values)
            _worker_instance.register_handler(TaskType.MANAGE_DATABASE_SCHEMAS, handle_manage_database_schemas)
            _worker_instance.register_handler(TaskType.BACKUP, handle_backup)
            _worker_instance.register_handler(TaskType.RESTORE, handle_restore)

            # Limit concurrent backup/restore tasks to avoid resource contention
            _worker_instance.set_type_concurrency_limit(TaskType.BACKUP, settings.BACKUP_MAX_CONCURRENT)
            _worker_instance.set_type_concurrency_limit(TaskType.RESTORE, settings.BACKUP_MAX_CONCURRENT)

            _worker_asyncio_task = asyncio.create_task(_worker_instance.run())
            logger.info("Task worker started in combined mode")

            # Start backup scheduler if enabled
            if settings.BACKUP_SCHEDULER_ENABLED:
                try:
                    from opi.core.backup_scheduler import BackupScheduler

                    _backup_scheduler = BackupScheduler(task_service=task_service, cluster=settings.CLUSTER_MANAGER)
                    await _backup_scheduler.start()
                    app.state.backup_scheduler = _backup_scheduler
                except Exception as e:
                    logger.error("Failed to start backup scheduler: %s", e)

            # Start resource tuning scheduler if enabled
            from opi.services.catalog.resource_tuning.config import resource_tuning_config

            if resource_tuning_config().scheduler_enabled:
                try:
                    from opi.core.resource_tuning_scheduler import ResourceTuningScheduler

                    _tuning_scheduler = ResourceTuningScheduler(cluster=settings.CLUSTER_MANAGER)
                    await _tuning_scheduler.start()
                    app.state.resource_tuning_scheduler = _tuning_scheduler
                except Exception as e:
                    logger.error("Failed to start resource tuning scheduler: %s", e)

        except Exception as e:
            logger.error("Failed to start task worker: %s", e)
    else:
        # Frontend-only mode: still create task_service for API endpoints
        try:
            from opi.core.async_task_service import AsyncTaskService  # type: ignore[reportMissingImports]
            from opi.core.database_pools import get_database_pool

            get_database_pool("main")  # ensure the shared asyncpg pool is initialized
            task_service = AsyncTaskService(cluster=settings.CLUSTER_MANAGER)
            app.state.task_service = task_service

            from opi.services.oom_watcher import set_task_service

            set_task_service(task_service)
            logger.info("Task service initialized (worker disabled)")
        except Exception as e:
            logger.warning(f"Failed to initialize task service: {e}")

    # Initialize federation service if configured as master
    if settings.FEDERATION_ROLE == "master" and settings.FEDERATION_PEERS:
        try:
            from opi.core.federation_config import FederationRegistry
            from opi.core.federation_service import FederationService

            registry = FederationRegistry.from_settings(settings.FEDERATION_PEERS, settings.CLUSTER_MANAGER)
            fed_task_service = getattr(app.state, "task_service", None)
            if fed_task_service and registry.is_enabled():
                app.state.federation_service = FederationService(registry, fed_task_service)
                logger.info("Federation service initialized (master mode, %d peers)", len(registry.get_all_peers()))
            else:
                logger.warning("Federation peers configured but task_service not available or no peers")
        except Exception as e:
            logger.error(f"Failed to initialize federation service: {e}")

    # Start the run reaper (sweeps both console and job bundles) if either is enabled.
    if settings.DB_CONSOLE_ENABLED or settings.JOB_ENABLED:
        try:
            from opi.core.db_console_reaper import DbConsoleReaper

            _db_console_reaper = DbConsoleReaper(cluster=settings.CLUSTER_MANAGER)
            await _db_console_reaper.start()
            app.state.db_console_reaper = _db_console_reaper
        except Exception as e:
            logger.error("Failed to start database console reaper: %s", e)

    # Start the log watcher scheduler if enabled
    if settings.LOGWATCHER_ENABLED:
        try:
            from opi.core.logwatcher_scheduler import LogwatcherScheduler

            _logwatcher_scheduler = LogwatcherScheduler()
            await _logwatcher_scheduler.start()
            app.state.logwatcher_scheduler = _logwatcher_scheduler
        except Exception as e:
            logger.error("Failed to start log watcher scheduler: %s", e)

    # Start the sleep-mode sweeper if enabled
    if settings.SLEEP_MODE_SCHEDULER_ENABLED:
        try:
            from opi.services.catalog.sleep_mode.scheduler import SleepModeScheduler

            _sleep_mode_scheduler = SleepModeScheduler(cluster=settings.CLUSTER_MANAGER)
            await _sleep_mode_scheduler.start()
            app.state.sleep_mode_scheduler = _sleep_mode_scheduler
        except Exception as e:
            logger.error("Failed to start sleep-mode sweeper: %s", e)

    yield

    # Begin graceful drain: reject new task creation via API immediately
    from opi.core.shutdown import begin_drain

    begin_drain()

    # Stop backup scheduler
    backup_scheduler = getattr(app.state, "backup_scheduler", None)
    if backup_scheduler is not None:
        await backup_scheduler.stop()

    # Stop resource tuning scheduler
    resource_tuning_scheduler = getattr(app.state, "resource_tuning_scheduler", None)
    if resource_tuning_scheduler is not None:
        await resource_tuning_scheduler.stop()

    # Stop database console reaper
    db_console_reaper = getattr(app.state, "db_console_reaper", None)
    if db_console_reaper is not None:
        await db_console_reaper.stop()

    # Stop sleep-mode sweeper
    sleep_mode_scheduler = getattr(app.state, "sleep_mode_scheduler", None)
    if sleep_mode_scheduler is not None:
        await sleep_mode_scheduler.stop()

    # Stop log watcher scheduler
    logwatcher_scheduler = getattr(app.state, "logwatcher_scheduler", None)
    if logwatcher_scheduler is not None:
        await logwatcher_scheduler.stop()

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

    # Stop periodic task cleanup
    stop_periodic_cleanup()

    # Stop the project-store fallback reconcile poll
    stop_reconcile_poll()

    # Stop Git monitoring service
    if settings.ENABLE_GIT_MONITOR:
        try:
            await stop_git_monitoring()
            logger.info("Git monitoring service stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping Git monitoring service: {e}")

    # Close database connection pools
    try:
        await close_database_pools()
        logger.info("Database pools closed successfully")
    except Exception as e:
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
    except Exception as e:
        logger.error(f"Error disposing ORM async engine: {e}")

    # Shut down OpenTelemetry tracing (flush pending spans)
    from opi.core.tracing import shutdown_tracing

    shutdown_tracing()

    logger.info(f"Stopping application {PROJECT_NAME} version {VERSION}")
    logging.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="RIG Operations Manager API",
        description="GitOps Operations and Project Infrastructure API for self-service Kubernetes environments",
        summary=PROJECT_DESCRIPTION,
        version=VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
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
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        from fastapi.openapi.utils import get_openapi

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        # Add security scheme for X-API-Key header
        openapi_schema["components"]["securitySchemes"] = {
            "APIKeyHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "API key for project authentication",
            }
        }

        # Apply security to all API routes
        for path, methods in openapi_schema["paths"].items():
            if path.startswith("/api/"):
                for method in methods.values():
                    if isinstance(method, dict) and "operationId" in method:
                        method["security"] = [{"APIKeyHeader": []}]

        # Sort paths: V2 first, then v1, for clarity in docs
        paths = openapi_schema.get("paths", {})
        sorted_paths = dict(sorted(paths.items(), key=lambda p: (not p[0].startswith("/api/v2"), p[0])))
        openapi_schema["paths"] = sorted_paths

        # Add API version info
        openapi_schema["info"]["x-api-info"] = {
            "v1_status": "deprecated - use /api/v2 endpoints",
            "v2_status": "current - recommended",
        }

        app.openapi_schema = openapi_schema
        return app.openapi_schema

    app.openapi = custom_openapi

    # Add middleware in the correct order (reverse order of execution)
    # ProxyHeadersMiddleware must be last (runs first) to set correct scheme from X-Forwarded-Proto
    from opi.middleware.maintenance import MaintenanceMiddleware
    from opi.utils.csrf import CSRFMiddleware

    # Log all unhandled exceptions through Python logging so they appear in
    # kubectl logs.  Starlette's ServerErrorMiddleware only prints to stderr
    # via traceback.print_exc() which may not reach the log stream.
    @app.exception_handler(Exception)
    async def _log_unhandled_exceptions(request, exc):  # type: ignore[no-untyped-def]
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        raise exc

    @app.exception_handler(StarletteHTTPException)
    async def _not_found_page(request, exc):  # type: ignore[no-untyped-def]
        """Serve a 404 as a page to a browser and as JSON to everything else.

        A browser asking for a page that is not there got the API's answer:
        ``{"detail":"Not Found"}`` on a white screen. The client says which one it
        wants, so read it: an /api path or a caller that does not ask for HTML keeps
        the JSON body every client parses today.
        """
        if exc.status_code != 404 or request.url.path.startswith("/api"):
            return await http_exception_handler(request, exc)
        if "text/html" not in request.headers.get("accept", ""):
            return await http_exception_handler(request, exc)
        return HTMLResponse(_NOT_FOUND_PAGE, status_code=404)

    from opi.middleware.security_headers import SecurityHeadersMiddleware

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
        SessionMiddleware,
        secret_key=settings.SECRET_KEY,
        same_site="lax",
        https_only=not settings.DEBUG,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        keycloak_url=settings.KEYCLOAK_URL,
        prometheus_url=settings.PROMETHEUS_EXTERNAL_URL or settings.PROMETHEUS_URL,
    )
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])  # type: ignore[arg-type]

    # Flow ID middleware - runs first (outermost) to tag all log lines for this request
    from opi.core.flow_id import set_flow_id

    @app.middleware("http")
    async def flow_id_middleware(request, call_next):  # type: ignore[no-untyped-def]
        set_flow_id("req")
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
    app.include_router(sleep_mode_router, include_in_schema=True)  # Sleep-mode wake/status (waker pod)
    app.include_router(v2_router, include_in_schema=True)  # V2 async API endpoints
    app.include_router(task_router, include_in_schema=True)  # Async task status API
    app.include_router(federation_router, include_in_schema=True)  # Federation peers/health
    app.include_router(admin_router, include_in_schema=True)  # Admin cleanup/reconciliation API
    app.include_router(prometheus_router, include_in_schema=False)  # Prometheus /metrics scrape endpoint
    app.include_router(invite_router, include_in_schema=False)  # Exclude from OpenAPI docs (public invite flow)
    app.include_router(web_router, include_in_schema=False)  # Exclude from OpenAPI docs

    # De assets van het componentensysteem liggen verspreid over meerdere geinstalleerde
    # pakketten (de kern plus elk design system), vandaar een route met meerdere wortels
    # in plaats van een mount.
    from opi.core.templates_lotc import resolve_lotc_static
    from opi.web.lotc_router import router as lotc_web_router

    app.include_router(lotc_web_router, include_in_schema=False)

    @app.get("/static/lotc/{rel:path}", include_in_schema=False)
    async def lotc_static(rel: str) -> FileResponse:
        resolved = resolve_lotc_static(rel)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Static asset not found")
        return FileResponse(resolved)

    logger.info("LOTC static files served at /static/lotc/")

    # Mount regular static files last (more general path)
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.exists(static_dir):
        app.mount("/static", CacheControlledStaticFiles(directory=static_dir), name="static")
        logger.info(f"Regular static files mounted at /static from {static_dir}")

    # Favicon at the root path (browsers request /favicon.ico automatically)
    favicon_path = os.path.join(static_dir, "favicon.ico")

    @app.get("/favicon.ico", include_in_schema=False, response_class=FileResponse)
    async def favicon():
        """Serve favicon from the expected root path."""
        return FileResponse(favicon_path, media_type="image/x-icon")

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
    async def liveness_check():
        """Liveness probe for Kubernetes - always returns OK."""
        return {"status": "ok"}

    # Version info - public, so anyone (and the E2E suite) can see which build is running.
    @app.get("/version", include_in_schema=True, tags=["meta"], response_class=JSONResponse)
    async def version_info():
        """Return the running build's version metadata (version, commit, branch, ...)."""
        from opi.core.version import get_version_info

        return get_version_info()

    # Readiness probe - only OK when all services are up
    @app.get("/readyz", include_in_schema=False, response_class=JSONResponse)
    async def readiness_check():
        """Readiness probe for Kubernetes - returns OK only when all services are ready."""
        from opi.core.readiness import get_readiness_state

        readiness = get_readiness_state()
        if readiness.is_ready:
            return {"status": "ok"}
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "services": readiness.summary()},
        )

    return app


app = create_app()


# Conditional startup for different development modes
if __name__ == "__main__":
    import uvicorn

    from opi.core.config import settings

    if settings.DEBUG_MODE == "debug":
        # Debug mode: Start with debugpy, no reload
        import debugpy

        debugpy.listen(("0.0.0.0", 5678))
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
