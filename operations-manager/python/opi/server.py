import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import jinja_roos_components
from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from opi.api.auth_routes import auth_router
from opi.api.backup_router import backup_router
from opi.api.image_router import image_router
from opi.api.invite_routes import invite_router
from opi.api.logs_router import logs_router
from opi.api.logs_websocket_router import logs_websocket_router
from opi.api.metrics_router import metrics_router
from opi.api.prometheus_router import prometheus_router
from opi.api.resource_router import resource_router
from opi.api.restore_router import restore_router
from opi.api.router import api_router
from opi.core.config import PROJECT_DESCRIPTION, PROJECT_NAME, VERSION, settings
from opi.core.database_pools import close_database_pools

# Initialize logging first, before any other imports that might log
from opi.core.early_logging import initialize_logging  # noqa: F401 (side-effect import)
from opi.core.git_monitor import start_git_monitoring, stop_git_monitoring
from opi.core.startup import run_startup_tasks
from opi.core.task_manager import start_periodic_cleanup, stop_periodic_cleanup
from opi.middleware.authorization import AuthorizationMiddleware
from opi.web.router import web_router

logger = logging.getLogger(__name__)

STATIC_DIR_ROOS = Path(jinja_roos_components.__file__).parent / "static" / "roos" / "dist"


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

    yield

    # Stop peak memory tracking
    from opi.core.metrics import stop_peak_memory_tracking

    stop_peak_memory_tracking()

    # Stop periodic task cleanup
    stop_periodic_cleanup()

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

        app.openapi_schema = openapi_schema
        return app.openapi_schema

    app.openapi = custom_openapi

    # Add middleware in the correct order (reverse order of execution)
    # ProxyHeadersMiddleware must be last (runs first) to set correct scheme from X-Forwarded-Proto
    from opi.middleware.maintenance import MaintenanceMiddleware
    from opi.utils.csrf import CSRFMiddleware

    app.add_middleware(CSRFMiddleware)
    app.add_middleware(AuthorizationMiddleware)
    app.add_middleware(MaintenanceMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])  # type: ignore[arg-type]

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
    app.include_router(metrics_router, include_in_schema=True)  # Include in OpenAPI docs
    app.include_router(logs_router, include_in_schema=True)  # Include in OpenAPI docs
    app.include_router(logs_websocket_router, include_in_schema=False)  # WebSocket for log streaming
    app.include_router(resource_router, include_in_schema=True)  # Resource tuning & sanitization
    app.include_router(prometheus_router, include_in_schema=False)  # Prometheus /metrics scrape endpoint
    app.include_router(invite_router, include_in_schema=False)  # Exclude from OpenAPI docs (public invite flow)
    app.include_router(web_router, include_in_schema=False)  # Exclude from OpenAPI docs

    # Mount ROOS component assets - use a simpler approach
    try:
        from jinja_roos_components import get_static_files_path

        roos_static_path = get_static_files_path()

        # Just mount the entire static directory
        if os.path.exists(roos_static_path):
            app.mount("/static/roos/dist", StaticFiles(directory=STATIC_DIR_ROOS), name="roos")
            logger.info(f"ROOS static files mounted at /static/roos from {roos_static_path}")
        else:
            logger.error(f"ROOS static path does not exist: {roos_static_path}")
    except ImportError as e:
        logger.warning(f"jinja-roos-components not available: {e}")
    except Exception as e:
        logger.error(f"Error mounting ROOS static files: {e}")

    # Mount regular static files last (more general path)
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.exists(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.info(f"Regular static files mounted at /static from {static_dir}")

    # Liveness probe - always OK (keeps the pod alive)
    @app.get("/health", include_in_schema=False, response_class=JSONResponse)
    @app.get("/healthz", include_in_schema=False, response_class=JSONResponse)
    async def liveness_check():
        """Liveness probe for Kubernetes - always returns OK."""
        return {"status": "ok"}

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
        logger.info("🔥 Hot-reload mode: File changes will auto-reload")
        uvicorn.run(
            "opi.server:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            reload_dirs=["/app/opi", "/app/templates", "/app/manifests"],
        )
    else:
        # Production mode: No reload, no debugging
        # Use asyncio event loop instead of uvloop to avoid zombie process accumulation
        # when running as PID 1 in a container (uvloop's libuv-based child reaping is
        # unreliable without a proper init process)
        uvicorn.run(app, host="0.0.0.0", port=8000, loop="asyncio")
