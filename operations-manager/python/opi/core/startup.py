"""
Startup logic for the Operations Manager application.

This module handles startup tasks like ensuring namespaces exist from project files,
setting up shared SOPS keys, and other initialization tasks.
"""

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from opi.bootstrap.keycloak_setup import setup_keycloak
from opi.connectors.git import (
    create_git_connector_for_project_files,
)
from opi.connectors.kubectl import KubectlConnector
from opi.connectors.minio_mc import create_minio_connector
from opi.connectors.prometheus import PrometheusConnector, create_prometheus_connector
from opi.connectors.subdomain import SUBDOMAIN_REGISTRY_TABLE_SQL
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.core.database_pools import initialize_database_pools
from opi.core.keycloak_client_startup import ensure_keycloak_credentials
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services.project_service import get_project_service, initialize_project_service
from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)


async def create_subdomain_registry_table() -> None:
    """Create the subdomain_registry table if it doesn't exist.

    This table is used by the nice URL feature to track globally unique subdomains.
    """
    from opi.core.database_pools import get_database_pool

    pool = get_database_pool("main")
    conn = await pool.acquire()
    try:
        await conn.execute(SUBDOMAIN_REGISTRY_TABLE_SQL)
        logger.debug("Subdomain registry table and indexes created/verified")
    finally:
        await pool.release(conn)


@retry(
    stop=stop_after_attempt(10),  # Try up to 10 times
    wait=wait_exponential(multiplier=2, min=4, max=60),  # Exponential backoff: 4s, 8s, 16s, 32s, 60s, 60s...
    retry=retry_if_exception_type(
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            ConnectionError,
            OSError,
        )
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    after=after_log(logger, logging.INFO),
)
async def wait_for_keycloak_availability() -> bool:
    """
    Wait for Keycloak to become available with exponential backoff retry.

    This function will retry up to 10 times with exponential backoff to handle
    situations where Keycloak is not yet ready during application startup.

    Returns:
        True if Keycloak is available

    Raises:
        Exception: If Keycloak is not available after all retry attempts
    """
    logger.info("Checking Keycloak availability...")

    try:
        keycloak = await create_keycloak_connector()

        # Try a simple API call to check if Keycloak is responding
        # We'll try to get the master realm info as a basic health check
        await keycloak.get_realm("master")

        logger.info("Keycloak is available and responding")
        return True

    except Exception as e:
        logger.warning(f"Keycloak not yet available: {e}")
        raise  # This will trigger the retry


def should_retry_keycloak_error(exception):
    """
    Determine if a Keycloak operation should be retried.

    We should NOT retry 404 errors since they are valid responses
    indicating that a resource doesn't exist.
    """
    if isinstance(exception, httpx.HTTPStatusError):
        # Don't retry 404 (Not Found) - it's a valid response for existence checks
        if exception.response.status_code == 404:
            return False
        # Don't retry client errors (4xx) except for 404 which we already handled
        if 400 <= exception.response.status_code < 500:
            return False
        # Retry server errors (5xx)
        return exception.response.status_code >= 500

    # Retry network/connection errors
    return isinstance(
        exception, (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, ConnectionError)
    )


@retry(
    stop=stop_after_attempt(5),  # Try up to 5 times for realm/provider operations
    wait=wait_exponential(multiplier=1, min=2, max=10),  # 2s, 4s, 8s, 10s, 10s
    retry=should_retry_keycloak_error,
    before_sleep=before_sleep_log(logger, logging.WARNING),
    after=after_log(logger, logging.INFO),
)
async def keycloak_operation_with_retry(operation_func, *args, **kwargs):
    """
    Execute a Keycloak operation with retry logic.

    This wrapper handles transient errors that might occur during Keycloak operations
    even after the service is available (e.g., temporary database locks, etc.).

    Args:
        operation_func: The async function to execute
        *args: Arguments to pass to the function
        **kwargs: Keyword arguments to pass to the function

    Returns:
        Result of the operation function
    """
    logger.debug(f"Executing Keycloak operation: {operation_func.__name__}")

    try:
        result = await operation_func(*args, **kwargs)
        logger.debug(f"Keycloak operation {operation_func.__name__} completed successfully")
        return result
    except Exception as e:
        # Check if this is a 404 error (valid response for existence checks)
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
            logger.debug(f"Keycloak operation {operation_func.__name__} returned 404 (resource not found)")
        else:
            logger.warning(f"Keycloak operation {operation_func.__name__} failed, will retry: {e}")
        raise  # This will trigger the retry (or not, based on our custom retry logic)


async def start_prometheus_reconnection_task() -> None:
    """
    Start a background task that attempts to reconnect to Prometheus.

    This task runs with exponential backoff, attempting to connect to Prometheus
    if the initial connection failed. Once connected, the task stops.
    The application continues to function without Prometheus - metrics will
    simply be unavailable until connection is established.
    """
    prometheus = create_prometheus_connector()

    if PrometheusConnector.is_connected:
        logger.info("Prometheus already connected, no background reconnection needed")
        return

    logger.info("Starting background Prometheus reconnection task")

    # Retry parameters: 10 attempts with exponential backoff (4s, 8s, 16s, 32s, 60s max)
    max_attempts = 10
    base_delay = 4
    max_delay = 60

    for attempt in range(1, max_attempts + 1):
        if PrometheusConnector.is_connected:
            logger.info("Prometheus connected, stopping reconnection task")
            return

        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
        logger.info(f"Prometheus reconnection attempt {attempt}/{max_attempts} in {delay}s")

        await asyncio.sleep(delay)

        if prometheus.reconnect():
            logger.info("Prometheus reconnection successful")
            return

    logger.warning(
        f"Prometheus reconnection failed after {max_attempts} attempts. "
        "Metrics will be unavailable. Manual restart may be required."
    )


def print_boot_banner():
    """Print a distinctive boot banner for easy log identification."""
    import datetime

    boot_time = datetime.datetime.now().isoformat()

    banner = f"""
{"=" * 80}
OPERATIONS MANAGER STARTING UP
{"=" * 80}
Boot Time: {boot_time}
Environment: {os.environ.get("ENVIRONMENT", "development")}
Debug Mode: {os.environ.get("DEBUG", "false")}
Git Monitoring: {os.environ.get("ENABLE_GIT_MONITOR", "false")}
{"=" * 80}
"""

    # Print to both stdout and logger
    print(banner)
    for line in banner.strip().split("\n"):
        if line.strip():
            logger.info(line)


async def get_project_files(repo_root_folder: str) -> list[str]:
    repo_root_folder = os.path.join(repo_root_folder, "projects")
    project_files: list[str] = []

    if not os.path.exists(repo_root_folder):
        logger.warning(f"Projects directory not found: {repo_root_folder}")
        return project_files

    for file in os.listdir(repo_root_folder):
        if file.endswith((".yaml", ".yml")):
            project_files.append(os.path.join("projects", file))

    logger.info(f"Found {len(project_files)} project files to process")
    return project_files


class ProjectRefreshState:
    """
    Global state manager for project refresh operations.

    Ensures that:
    - Projects are refreshed from Git at most once every REFRESH_TTL_SECONDS
    - Concurrent refresh requests wait for an in-progress refresh instead of triggering multiple
    """

    REFRESH_TTL_SECONDS = 30

    _instance: "ProjectRefreshState | None" = None

    def __init__(self) -> None:
        self.last_refresh_time: float = 0.0
        self.refresh_lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "ProjectRefreshState":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def is_stale(self) -> bool:
        return (time.time() - self.last_refresh_time) > self.REFRESH_TTL_SECONDS

    def mark_refreshed(self) -> None:
        self.last_refresh_time = time.time()


def get_project_refresh_state() -> ProjectRefreshState:
    return ProjectRefreshState.get_instance()


async def ensure_projects_fresh() -> None:
    """
    Ensure project data is fresh, refreshing from Git if stale.

    This function:
    - Returns immediately if data was refreshed within the last 30 seconds
    - Acquires a lock to prevent concurrent refresh operations
    - Refreshes from Git if data is stale
    - Other concurrent requests wait for the refresh to complete
    """
    state = get_project_refresh_state()

    if not state.is_stale():
        logger.debug("Project data is fresh, skipping refresh")
        return

    async with state.refresh_lock:
        # Double-check after acquiring lock (another request may have refreshed)
        if not state.is_stale():
            logger.debug("Project data was refreshed while waiting for lock")
            return

        logger.info("Project data is stale, refreshing from Git")
        await refresh_projects_from_git()
        state.mark_refreshed()


async def refresh_projects_from_git() -> int:
    """
    Refresh the project cache by fetching the latest from Git and reloading all project files.

    This function:
    1. Creates a new Git connector and fetches the latest changes
    2. Clears the existing project cache
    3. Reloads all project files from the Git repository
    4. Registers each project in the ProjectService

    Returns:
        Number of projects successfully loaded
    """
    logger.info("Refreshing projects from Git...")

    project_service = get_project_service()

    # Clear existing projects to reload fresh data
    project_service.clear_all_projects()

    # Create a shared Git connector that will be reused across all ProjectManagers
    shared_git_connector = None
    try:
        shared_git_connector = await create_git_connector_for_project_files("refresh projects from git")
        projects_repo_root_dir = await shared_git_connector.get_working_dir()
        project_files = await get_project_files(projects_repo_root_dir)
    except Exception as e:
        logger.error(f"Failed to get project files from Git: {e}")
        if shared_git_connector:
            await shared_git_connector.close()
        raise

    try:
        loaded_count = 0
        for project_file in project_files:
            # Each ProjectManager shares the git connector for project files
            # The connector ownership tracking ensures it won't be closed by individual managers
            project_manager = ProjectManager(
                project_file_relative_path=project_file,
                git_connector_for_project_files=shared_git_connector,
            )
            try:
                project_file_base_name = os.path.basename(project_file)
                logger.debug(f"Refreshing project file: {project_file_base_name}")

                # Load project data
                api_key = await project_manager.get_api_key()
                project_name = await project_manager.get_name()
                project_data = await project_manager.get_contents()

                # Register project with users and full project data
                project_service.register(
                    project_name, api_key, project_file_base_name, project_data.get("users", []), project_data
                )

                # Add project users to allowed emails list
                project_users = project_data.get("users", [])
                if project_users:
                    user_service = get_user_service()
                    project_user_emails = [u.get("email") for u in project_users if u.get("email")]
                    if project_user_emails:
                        user_service.add_allowed_emails(project_user_emails)

                loaded_count += 1
            except Exception as e:
                logger.error(f"Error refreshing project file {project_file}: {e}")
            finally:
                await project_manager.close()

        logger.info(f"Refreshed {loaded_count} projects from Git")
        return loaded_count
    finally:
        # Close the shared git connector now that all project managers are done
        if shared_git_connector:
            await shared_git_connector.close()
            logger.debug("Shared git connector for project files closed")


async def ensure_project_sops_secrets(project_data: Any, kubectl: KubectlConnector) -> bool:
    """
    Ensure that all project namespaces have project-specific SOPS secrets.

    This function:
    1. Checks all project namespaces
    2. For namespaces without SOPS secrets, creates project-specific keys
    3. Preserves existing project-specific keys

    Returns:
        True if all operations were successful, False otherwise
    """
    logger.info("Ensuring project-specific SOPS secrets in project namespaces")

    project_name = project_data.get("name")
    deployments = project_data.get("deployments", [])

    # Perform explicit recovery check for each namespace
    project_manager = create_project_manager()
    try:
        recovery_needed = False

        # Check each deployment namespace for missing SOPS secrets
        for deployment in (d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER):
            deployment_name = deployment.get("name")

            # TODO: namespace is too kubernetes specific; maybe 'target: 'shared' or target: 'unique'?
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment.get("namespace"))

            logger.info(f"Checking SOPS secret in namespace: {namespace}")

            # Check if SOPS secret exists in namespace
            existing_secret = await kubectl.get_sops_secret_from_namespace(namespace)
            if existing_secret:
                logger.info(f"SOPS secret already exists in namespace: {namespace}")
                continue

            logger.warning(f"Missing SOPS secret in namespace: {namespace} - attempting recovery")
            recovery_needed = True
    finally:
        await project_manager.close()

        # Try to recover from GitOps backup
        try:
            # TODO: missing git_connector ?
            recovered_keys = await project_manager._sops_handler.retrieve_project_sops_key_from_gitops(
                project_name, git_connector
            )

            if recovered_keys:
                private_key, public_key = recovered_keys
                logger.info(f"Successfully recovered SOPS key from GitOps backup for project: {project_name}")

                # Store recovered key in the namespace
                result = await project_manager._sops_handler.store_project_sops_key_in_namespace(
                    namespace, private_key, public_key
                )

                if result:
                    logger.info(f"Successfully restored SOPS secret to namespace: {namespace}")
                else:
                    logger.error(f"Failed to restore SOPS secret to namespace: {namespace}")
            else:
                logger.error(
                    f"No SOPS key backup found for project: {project_name} - cannot recover namespace: {namespace}"
                )

        except Exception as recovery_error:
            logger.error(f"Error during SOPS key recovery for project {project_name}: {recovery_error}")

    if recovery_needed:
        logger.info(f"Completed SOPS secret recovery process for project: {project_name}")
    else:
        logger.info(f"All SOPS secrets verified for project: {project_name}")

    return True


async def register_oauth_client_after_keycloak_setup(app) -> None:
    """
    Register the OAuth client after Keycloak setup is complete.

    This function should be called after Keycloak credentials are available
    to properly register the OAuth client for authentication.

    Args:
        app: The FastAPI application instance

    Raises:
        RuntimeError: If OAuth client registration fails
    """
    from opi.core.config import settings

    if not (settings.OIDC_CLIENT_ID and settings.OIDC_CLIENT_SECRET and settings.OIDC_DISCOVERY_URL):
        raise RuntimeError(
            f"Cannot register OAuth client - OIDC credentials missing after Keycloak setup. "
            f"Available: client_id={'Yes' if settings.OIDC_CLIENT_ID else 'No'}, "
            f"client_secret={'Yes' if settings.OIDC_CLIENT_SECRET else 'No'}, "
            f"discovery_url={'Yes' if settings.OIDC_DISCOVERY_URL else 'No'}"
        )

    oauth = app.state.oauth

    logger.info("Registering OAuth client with discovered credentials:")
    logger.info(f"  - client_id: {settings.OIDC_CLIENT_ID}")
    logger.info(
        f"  - client_secret: {'***' + settings.OIDC_CLIENT_SECRET[-4:] if settings.OIDC_CLIENT_SECRET else 'None'}"
    )
    logger.info(f"  - discovery_url: {settings.OIDC_DISCOVERY_URL}")

    oauth.register(  # type: ignore
        name="keycloak",
        client_id=settings.OIDC_CLIENT_ID,
        client_secret=settings.OIDC_CLIENT_SECRET,
        server_metadata_url=settings.OIDC_DISCOVERY_URL,
        client_kwargs={
            "scope": "openid profile email",
        },
    )


async def keycloak_client_exists_and_works() -> bool:
    """
    Ensure operations manager has valid Keycloak credentials.

    This function now delegates to the enhanced credential management logic
    that can create/retrieve credentials if they're missing or invalid.

    Returns:
        True if valid credentials are available, False otherwise
    """
    return await ensure_keycloak_credentials()


async def check_minio_availability() -> bool:
    """
    Check MinIO CLI availability and basic functionality.

    This function verifies that the mc CLI tool is installed and available
    for use by the MinIO connector.

    Returns:
        True if MinIO CLI is available and functional, False otherwise
    """
    logger.info("Checking MinIO CLI availability...")

    try:
        minio_connector = create_minio_connector()

        # Check if MC CLI is available
        if not minio_connector.is_mc_available:
            logger.error("MinIO CLI (mc) is not available - please ensure it's installed")
            return False

        # Test MC CLI functionality
        is_available = await minio_connector._test_mc_availability()
        if not is_available:
            logger.error("MinIO CLI (mc) is installed but not functioning properly")
            return False

        logger.info("MinIO CLI (mc) is available and functional")

        # Note: We don't test specific MinIO server connections here since they
        # need to be configured per-project with aliases. The CLI availability
        # check ensures the tool is ready when needed.

        return True

    except Exception as e:
        logger.error(f"Error checking MinIO CLI availability: {e}")
        return False


async def run_startup_tasks(app: FastAPI) -> bool:
    """
    Run all startup tasks for the application.

    Returns:
        True if all startup tasks completed successfully, False otherwise
    """
    from opi.core.config import settings

    skip_checks = settings.SKIP_STARTUP_CHECKS
    if skip_checks:
        logger.warning("SKIP_STARTUP_CHECKS=True - skipping namespace/Keycloak/MinIO checks for fast startup")

    logger.info("Running startup tasks...")

    # Initialize database connection pools (CRITICAL - app cannot function without this)
    try:
        await initialize_database_pools()
        logger.info("Database pools initialized successfully")
    except Exception as e:
        logger.critical(
            f"CRITICAL STARTUP FAILURE: Cannot initialize database pools. "
            f"Application requires database connectivity to function. Error: {e}"
        )
        raise RuntimeError(f"Database pool initialization failed: {e}") from e

    # Create subdomain registry table if it doesn't exist
    try:
        await create_subdomain_registry_table()
        logger.info("Subdomain registry table ensured")
    except Exception as e:
        logger.error(f"Failed to create subdomain registry table: {e}")
        # Non-critical - nice URLs won't work but app can continue

    # Initialize Prometheus connector (non-critical - metrics will be unavailable if it fails)
    # If not connected, start a background task to retry
    logger.info("Initializing Prometheus connector")
    create_prometheus_connector()
    if not PrometheusConnector.is_connected:
        logger.warning("Prometheus not available at startup, starting background reconnection task")
        # Store reference to prevent garbage collection of the background task
        app.state.prometheus_reconnect_task = asyncio.create_task(start_prometheus_reconnection_task())
    else:
        logger.info("Prometheus connected successfully")

    # Initialize the API key service for project API key registration
    initialize_project_service()

    # Initialize user service with allowed emails
    user_service = get_user_service()

    # Add default allowed emails for testing/development
    # TODO: In production, these should be loaded from configuration or environment variables
    default_allowed_emails = [
        "robbert.uittenbroek@rijksoverheid.nl",
    ]

    if default_allowed_emails:
        user_service.add_allowed_emails(default_allowed_emails)
        logger.info(f"Added {len(default_allowed_emails)} default allowed emails to user service")

    # If ALLOWED_EMAILS setting is configured, add those too
    if settings.ALLOWED_EMAILS:
        env_emails = [email.strip() for email in settings.ALLOWED_EMAILS.split(",") if email.strip()]
        if env_emails:
            user_service.add_allowed_emails(env_emails)
            logger.info(f"Added {len(env_emails)} allowed emails from ALLOWED_EMAILS setting")

    # Get the list of project files to process
    # Create a shared git connector that will be reused across all ProjectManagers
    shared_git_connector = None
    try:
        shared_git_connector = await create_git_connector_for_project_files("startup project files")
        projects_repo_root_dir = await shared_git_connector.get_working_dir()
        project_files = await get_project_files(projects_repo_root_dir)
    except Exception as e:
        logger.error(f"Failed to get project files list: {e}")
        if shared_git_connector:
            await shared_git_connector.close()
        raise

    try:
        all_successful = True
        for project_file in project_files:
            # Each ProjectManager shares the git connector for project files
            # The connector ownership tracking ensures it won't be closed by individual managers
            project_manager = ProjectManager(
                project_file_relative_path=project_file,
                git_connector_for_project_files=shared_git_connector,
            )
            try:
                project_file_base_name = os.path.basename(project_file)
                logger.info(f"Processing project file: {project_file_base_name}")

                # Skip namespace/SOPS checks when SKIP_STARTUP_CHECKS is enabled
                if not skip_checks:
                    await project_manager.check_and_create_namespaces()
                    await project_manager.check_and_create_sops_secrets_in_namespaces()
                else:
                    logger.debug(f"Skipping namespace/SOPS checks for {project_file_base_name}")

                # Load and register API key for this project (always needed)
                api_key = await project_manager.get_api_key()
                project_name = await project_manager.get_name()
                project_service = get_project_service()

                # Load project data to get users
                project_data = await project_manager.get_contents()

                # Register project with users and full project data
                project_service.register(
                    project_name, api_key, project_file_base_name, project_data.get("users", []), project_data
                )

                # Add project users to allowed emails list
                project_users = project_data.get("users", [])
                if project_users:
                    project_user_emails = [u.get("email") for u in project_users if u.get("email")]
                    if project_user_emails:
                        user_service.add_allowed_emails(project_user_emails)
            except Exception as e:
                logger.error(f"Error processing project file {project_file}: {e}")
                all_successful = False
            finally:
                # Close all resources including database connections
                await project_manager.close()

        # Log all allowed emails for visibility (after all project users have been added)
        all_allowed_emails = user_service.get_allowed_emails()
        if all_allowed_emails:
            logger.info(f"Allowed user emails ({len(all_allowed_emails)}): {', '.join(sorted(all_allowed_emails))}")

        # Always ensure operations manager has valid Keycloak credentials
        # This is needed for the operations manager's own authentication system
        logger.info("Ensuring operations manager has valid Keycloak credentials")
        credentials_success = await keycloak_client_exists_and_works()
        if credentials_success:
            logger.info("Operations manager Keycloak credentials ensured successfully")
        else:
            logger.error("Failed to ensure operations manager Keycloak credentials")
            all_successful = False

        # Always register OAuth client - this is internal configuration, not external resource creation
        if app:
            logger.info("Registering OAuth client")
            await register_oauth_client_after_keycloak_setup(app)
            logger.info("OAuth client registration completed successfully")
        else:
            raise RuntimeError("No app instance provided - cannot register OAuth client")

        # Skip external resource creation when SKIP_STARTUP_CHECKS is enabled
        if not skip_checks:
            logger.info("Checking MinIO CLI availability")
            minio_success = await check_minio_availability()
            if minio_success:
                logger.info("MinIO CLI check completed successfully")
            else:
                logger.error("MinIO CLI check failed")
                all_successful = False

            logger.info("Setting up Keycloak (realm, SSO, scopes, and operations client)")
            keycloak_success = await setup_keycloak()
            if not keycloak_success:
                raise RuntimeError("Keycloak setup failed - cannot proceed without authentication")

            logger.info("Complete Keycloak setup completed successfully")
        else:
            logger.warning("Skipped external resource creation (SKIP_STARTUP_CHECKS=True)")

        # API keys are now loaded inline during project file processing above
        logger.info("Project API keys loaded during project processing")

        if all_successful:
            logger.info("All startup tasks completed successfully")
        else:
            logger.warning("Some startup tasks failed, but application will continue")

        return all_successful
    except Exception as e:
        logger.error(f"Startup failed with error: {e}")
        raise
    finally:
        # Close the shared git connector now that all project managers are done
        if shared_git_connector:
            await shared_git_connector.close()
            logger.debug("Shared git connector for project files closed")
