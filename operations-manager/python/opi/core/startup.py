"""
Startup logic for the Operations Manager application.

This module handles startup tasks like ensuring namespaces exist from project files,
setting up shared SOPS keys, and other initialization tasks.
"""

import logging
import os
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
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.core.database_pools import initialize_database_pools
from opi.core.keycloak_client_startup import ensure_keycloak_credentials
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services.project_service import get_project_service, initialize_project_service
from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)


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
        # DO NOT continue startup - the application cannot work without database pools
        # The retry logic in initialize_database_pools() has already attempted multiple times
        raise RuntimeError(f"Database pool initialization failed: {e}") from e

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

    # If ALLOWED_EMAILS environment variable is set, add those too
    env_allowed_emails = os.environ.get("ALLOWED_EMAILS")
    if env_allowed_emails:
        env_emails = [email.strip() for email in env_allowed_emails.split(",") if email.strip()]
        if env_emails:
            user_service.add_allowed_emails(env_emails)
            logger.info(f"Added {len(env_emails)} allowed emails from ALLOWED_EMAILS environment variable")

    # Initialize git connector variable for cleanup
    git_connector_for_project_files = None

    try:
        git_connector_for_project_files = await create_git_connector_for_project_files("all project files")
        projects_repo_root_dir = await git_connector_for_project_files.get_working_dir()
        project_files = await get_project_files(projects_repo_root_dir)

        all_successful = True
        for project_file in project_files:
            project_manager = ProjectManager(
                git_connector_for_project_files=git_connector_for_project_files,
                project_file_relative_path=project_file,
            )
            try:
                project_file_base_name = os.path.basename(project_file)
                logger.info(f"Processing project file: {project_file_base_name}")
                await project_manager.check_and_create_namespaces()
                await project_manager.check_and_create_sops_secrets_in_namespaces()

                # Load and register API key for this project
                api_key = await project_manager.get_api_key()
                project_name = await project_manager.get_name()
                project_service = get_project_service()

                # Load project data to get users
                project_data = await project_manager.get_contents()

                # Register project with users and full project data
                project_service.register(
                    project_name, api_key, project_file_base_name, project_data.get("users", []), project_data
                )
            except Exception as e:
                logger.error(f"Error processing project file {project_file}: {e}")
            finally:
                await project_manager.close_git_connectors_for_deployments()

        logger.info("Checking MinIO CLI availability")
        minio_success = await check_minio_availability()
        if minio_success:
            logger.info("MinIO CLI check completed successfully")
        else:
            logger.error("MinIO CLI check failed")
            all_successful = False

        logger.info("Ensuring operations manager has valid Keycloak credentials")
        credentials_success = await keycloak_client_exists_and_works()
        if credentials_success:
            logger.info("Operations manager Keycloak credentials ensured successfully")
        else:
            logger.error("Failed to ensure operations manager Keycloak credentials")
            all_successful = False

        logger.info("Setting up Keycloak (realm, SSO, scopes, and operations client)")
        keycloak_success = await setup_keycloak()
        if not keycloak_success:
            raise RuntimeError("Keycloak setup failed - cannot proceed without authentication")

        logger.info("Complete Keycloak setup completed successfully")

        # Register OAuth client now that OIDC credentials are available
        if app:
            logger.info("Registering OAuth client with post-setup credentials")
            await register_oauth_client_after_keycloak_setup(app)
            logger.info("OAuth client registration completed successfully")
        else:
            raise RuntimeError("No app instance provided - cannot register OAuth client")

        # API keys are now loaded inline during project file processing above
        logger.info("Project API keys loaded during project processing")

        if all_successful:
            logger.info("All startup tasks completed successfully")
        else:
            logger.warning("Some startup tasks failed, but application will continue")

        return all_successful

    finally:
        # Clean up the git connector to remove temporary repository
        if git_connector_for_project_files is not None:
            try:
                await git_connector_for_project_files.close()
                logger.debug("Main git connector cleaned up successfully")
            except Exception as e:
                logger.warning(f"Error cleaning up main git connector: {e}")
