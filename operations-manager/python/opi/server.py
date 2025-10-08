import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from opi.api.auth_routes import auth_router
from opi.api.router import api_router
from opi.core.config import PROJECT_DESCRIPTION, PROJECT_NAME, VERSION, settings
from opi.core.database_pools import close_database_pools

# Initialize logging first, before any other imports that might log
from opi.core.early_logging import initialize_logging  # noqa: F401
from opi.core.git_monitor import start_git_monitoring, stop_git_monitoring
from opi.core.startup import run_startup_tasks
from opi.middleware.authorization import AuthorizationMiddleware
from opi.web.router import web_router

logger = logging.getLogger(__name__)


# todo(berry): move lifespan to own file
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Print distinctive boot banner
    from opi.core.startup import print_boot_banner

    print_boot_banner()

    # Logging is already initialized via early_logging import
    logger.info(f"Starting {PROJECT_NAME} version {VERSION}")
    # logger.info(f"Settings: {mask.secrets(get_settings().model_dump())}")

    # Run startup tasks (namespace creation, SOPS secrets, etc.)
    try:
        await run_startup_tasks(app)
        logger.info("Startup tasks completed")
    except Exception as e:
        logger.error(f"Error in startup tasks: {e}")
        # Continue startup even if some tasks fail

    # Start Git monitoring service
    if settings.ENABLE_GIT_MONITOR:
        try:
            await start_git_monitoring(app)
            logger.info("Git monitoring service started successfully")
        except Exception as e:
            logger.error(f"Failed to start Git monitoring service: {e}")

    yield

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
    app.add_middleware(AuthorizationMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

    # Initialize OAuth client (registration happens during startup after Keycloak setup)
    oauth = OAuth()
    app.state.oauth = oauth
    logger.info("OAuth client initialized - registration will happen after Keycloak setup")

    # Include routers - only API router will appear in OpenAPI docs
    app.include_router(auth_router, include_in_schema=False)  # Exclude from OpenAPI docs
    app.include_router(api_router, include_in_schema=True)  # Include in OpenAPI docs
    app.include_router(web_router, include_in_schema=False)  # Exclude from OpenAPI docs

    # Mount ROOS component assets - use a simpler approach
    try:
        from jinja_roos_components import get_static_files_path

        roos_static_path = get_static_files_path()

        # Just mount the entire static directory
        if os.path.exists(roos_static_path):
            app.mount("/static/roos", StaticFiles(directory=roos_static_path), name="roos-static")
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

    return app


app = create_app()
