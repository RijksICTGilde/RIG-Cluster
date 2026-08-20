"""
Startup logic for the Operations Manager application.

This module handles startup tasks like ensuring namespaces exist from project files,
setting up shared SOPS keys, and other initialization tasks.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

import aiohttp
import httpx

if TYPE_CHECKING:
    from fastapi import FastAPI

    from opi.connectors.kubectl import KubectlConnector
    from opi.core.readiness import ReadinessState
from keycloak.exceptions import KeycloakError
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from opi.bootstrap.keycloak_setup import setup_keycloak
from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnectionError, KubectlExecutionError, create_kubectl_connector
from opi.connectors.minio_mc import create_minio_connector
from opi.connectors.prometheus import get_metrics_connector
from opi.core.caa_reconciler import reconcile_caa_records
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.core.database_pools import initialize_database_pools
from opi.core.keycloak_client_startup import ensure_keycloak_credentials
from opi.core.no_mail_reconciler import reconcile_no_mail_records
from opi.core.project_schema import check_schema_versions
from opi.core.version import set_running_image
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services.project_service import initialize_project_service
from opi.services.project_store import get_project_store
from opi.services.schema_migration import SCHEMA_VERSIONS
from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)

# Name of this application's container in its own pod (bootstrap/rig-system deployment).
CONTAINER_NAME = "operations-manager"


def _run_alembic_migrations() -> None:
    """Run Alembic migrations to bring the database schema to head.

    This replaces the individual CREATE TABLE IF NOT EXISTS calls.
    Alembic's baseline migration uses IF NOT EXISTS so it is safe
    for databases that already have the tables.
    """
    import pathlib

    from alembic import command
    from alembic.config import Config

    ini_path = pathlib.Path(__file__).resolve().parents[2] / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    command.upgrade(alembic_cfg, "head")


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
            httpx.HTTPStatusError,
            KeycloakError,  # python-keycloak wraps 503/connection errors in its own exception types
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
        # Prefer OPI's service account; on a fresh cluster that client does not
        # exist yet (it is created later in setup_keycloak), so fall back to the
        # admin-password connection just to confirm the server is reachable.
        keycloak = await create_keycloak_connector()
        if await keycloak.connection_works():
            logger.info("Keycloak is available and responding")
            return True

        admin = await create_keycloak_connector(use_client_credentials=False)
        if await admin.connection_works():
            logger.info("Keycloak is available and responding (admin-password fallback)")
            return True

        raise RuntimeError("Keycloak did not respond via client-credentials or admin password")

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
        exception, httpx.ConnectError | httpx.TimeoutException | httpx.RemoteProtocolError | ConnectionError
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
    metrics_connector = await get_metrics_connector()

    if metrics_connector.is_connected:
        logger.info("Metrics connector already connected, no background reconnection needed")
        return

    logger.info("Starting background metrics reconnection task")

    # Retry parameters: 10 attempts with exponential backoff (4s, 8s, 16s, 32s, 60s max)
    max_attempts = 10
    base_delay = 4
    max_delay = 60

    for attempt in range(1, max_attempts + 1):
        if metrics_connector.is_connected:
            logger.info("Metrics connector connected, stopping reconnection task")
            return

        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
        logger.info(f"Metrics reconnection attempt {attempt}/{max_attempts} in {delay}s")

        await asyncio.sleep(delay)

        if metrics_connector.reconnect():
            logger.info("Metrics connector reconnection successful")
            return

    logger.warning(
        f"Metrics connector reconnection failed after {max_attempts} attempts. "
        "Metrics will be unavailable. Manual restart may be required."
    )


def print_boot_banner():
    """Print a distinctive boot banner for easy log identification."""
    import datetime

    boot_time = datetime.datetime.now(tz=datetime.UTC).isoformat()

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


# ProjectRefreshState and ensure_projects_fresh() were removed.
#
# They made every web page render potentially trigger a git fetch, on a 30-second
# TTL, so data freshness depended on how recently someone had opened a page --
# API-only consumers got no freshness at all, and the TTL guaranteed nothing in
# particular. Reads come from the ProjectStore, and the store's cache is
# write-through for everything ZAD writes, so no polling is needed to stay correct
# for ZAD's own changes.
#
# What genuinely needs detecting is an edit made outside ZAD (by hand, or from
# another cluster). store.reconcile() is called explicitly by the refresh action,
# and a slow fallback poll (start_reconcile_poll in project_store, every
# PROJECT_STORE_RECONCILE_INTERVAL_SECONDS) bounds how long such an edit --
# notably an out-of-band revocation of a member or invite key -- can go unseen.
# Both start with an ls-remote check so an idle tick costs nothing.


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


async def ensure_platform_mail_account() -> bool:
    """Make sure ZAD's own account exists on the mail relay.

    Here and not in a project run, because ZAD is not a project: it has no project file to
    hang an account on and no processing cycle that would create one. Setting up the relay
    is when the platform account belongs, and a boot IS that moment -- it repeats harmlessly
    and repairs an account that was removed by hand.

    Non-critical: a cluster without a relay simply has no platform mail yet, and that must
    not stop OPI from booting. What it does block is password reset and invite mail, which
    is why the outcome is logged either way.

    "Non-critical" only holds if every way this can fail is caught HERE: ``server.py`` awaits
    ``run_startup_tasks`` without a guard, so an exception escaping this function takes the
    boot down and phases 4 and 5 (Keycloak, OAuth) never run. A relay that is configured but
    unreachable does not raise ``MailRelayError`` -- aiohttp raises its own
    ``ClientConnectorError`` before there is any HTTP answer to turn into one. So catch what
    ``check_minio_availability`` right below catches too: the transport errors as well, and
    the kubectl failure from writing the account's Secret (a cluster where that is refused
    must still boot).

    That last one has two shapes, and both are normal on a fresh cluster: kubectl raises
    ``KubectlConnectionError`` -- NOT a subclass of ``KubectlExecutionError`` -- when the
    API server is unreachable, which is exactly the state its own retry loop exists for.
    And ``create_mail_connector`` decrypts ``MAIL_RELAY_ADMIN_PASSWORD``, so an admin
    password that is not (yet) decryptable arrives here as a plain ``ValueError``; the
    first boot after someone sets ``MAIL_RELAY_API_URL`` is the likely moment for it.
    """
    from opi.connectors.kubectl import KubectlConnectionError, KubectlExecutionError
    from opi.connectors.mail import MailRelayError
    from opi.manager.mail_manager import MailManager

    try:
        account = await MailManager.ensure_platform_account()
    except (
        MailRelayError,
        KubectlExecutionError,
        KubectlConnectionError,
        ValueError,
        aiohttp.ClientError,
        OSError,
    ) as error:
        logger.error(f"Platform-mailaccount kon niet worden ingericht: {error}")
        return False
    if account is None:
        return False
    logger.info(f"Platform-mailaccount {account.username} staat klaar ({account.from_address})")
    return True


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


async def _setup_database(readiness: ReadinessState) -> bool:
    """Initialize database pools. Returns True on success.

    Note: Database migrations are now primarily handled by the Docker entrypoint
    (docker-entrypoint.sh) which runs 'alembic upgrade head' before starting the app.
    This function provides a backup/sanity check to verify the schema is ready.
    """
    try:
        from opi.core.database_pools import is_database_available

        if is_database_available():
            readiness.database.mark_ready()
            return True

        await initialize_database_pools()

        # Run Alembic migrations as a backup check (primary responsibility now in entrypoint)
        try:
            logger.info("Running Alembic migrations as sanity check (primary migrations handled by entrypoint)")
            _run_alembic_migrations()
            logger.info("Alembic migration sanity check completed")
        except Exception as e:
            # Log but don't fail - entrypoint already ran migrations before this code executed
            logger.debug(f"Alembic sanity check skipped or encountered non-fatal issue: {e}")

        readiness.database.mark_ready()
        return True
    except Exception as e:
        readiness.database.mark_failed(str(e))
        return False


async def _setup_projects(readiness: ReadinessState, app: FastAPI, skip_checks: bool) -> bool:
    """Load project files from Git. Returns True on success."""
    try:
        # Initialize the API key service for project API key registration
        initialize_project_service()

        user_service = get_user_service()

        default_allowed_emails = [
            "robbert.uittenbroek@rijksoverheid.nl",
        ]
        if default_allowed_emails:
            user_service.add_allowed_emails(default_allowed_emails)

        if settings.ALLOWED_EMAILS:
            env_emails = [email.strip() for email in settings.ALLOWED_EMAILS.split(",") if email.strip()]
            if env_emails:
                user_service.add_allowed_emails(env_emails)

        # Load platform users from the users database table into the allowlist
        try:
            from opi.services.user_admin_service import UserAdminService

            admin_service = UserAdminService()
            db_users = await admin_service.list_users()
            if db_users:
                db_emails = [u["email"] for u in db_users if u.get("email")]
                if db_emails:
                    user_service.add_allowed_emails(db_emails)
                    logger.info(f"Loaded {len(db_emails)} platform users from database into allowlist")
        except Exception as e:
            logger.warning(f"Could not load platform users from database: {e}")

        default_admin_emails = [
            "robbert.uittenbroek@rijksoverheid.nl",
        ]
        get_user_service().add_platform_admins(default_admin_emails)

        if settings.ADMIN_EMAILS:
            env_admin_emails = [email.strip() for email in settings.ADMIN_EMAILS.split(",") if email.strip()]
            if env_admin_emails:
                get_user_service().add_platform_admins(env_admin_emails)

        # Loading every project into the cache is the store's job, and only the store's.
        # This used to be a second, parallel loader here (walk the working copy, build a
        # ProjectManager per file, call project_service.register) alongside
        # refresh_projects_from_git doing the same thing a third way. Three loaders meant
        # three chances for the cache to disagree with git.
        store = get_project_store()
        await store.bootstrap()

        # Provisioning is a separate concern from loading: it needs the project list, not
        # the file walk, so it now runs off the cache the store just populated.
        if not skip_checks:
            # Read the cluster's namespaces once instead of per project-deployment. This
            # loop used to take 70 of the 83 seconds it took to boot, nearly all of it
            # spent forking kubectl: 127 `get namespace` plus 127 `label namespace` for
            # 44 distinct namespaces that were already correct.
            known_namespace_labels: dict[str, str] | None = None
            try:
                known_namespace_labels = await create_kubectl_connector().get_namespace_label_map(
                    "argocd.argoproj.io/managed-by"
                )
                logger.info(f"Read {len(known_namespace_labels)} namespaces in one call for the startup check")
            except Exception as e:
                # Fall back to the per-namespace path rather than skipping the check.
                logger.warning(f"Could not pre-read namespaces, falling back to per-namespace checks: {e}")

            for project in store.get_all():
                project_manager = ProjectManager(project_file_relative_path=f"projects/{project.filename}")
                try:
                    logger.info(f"Checking namespaces and secrets for project: {project.filename}")
                    await project_manager.check_and_create_namespaces(known_namespace_labels=known_namespace_labels)
                    await project_manager.check_and_create_sops_secrets_in_namespaces()
                except Exception as e:
                    logger.error(f"Error checking project {project.filename}: {e}")
                finally:
                    await project_manager.close()

        all_allowed_emails = user_service.get_allowed_emails()
        if all_allowed_emails:
            logger.info(f"Allowed user emails ({len(all_allowed_emails)}): {', '.join(sorted(all_allowed_emails))}")

        # The connector is the ProjectStore's warm working copy and is owned by
        # the store: closing it would delete the working directory that every
        # later request reuses. It deliberately outlives this function.

        readiness.projects.mark_ready()
        return True
    except Exception as e:
        readiness.projects.mark_failed(str(e))
        return False


async def _setup_keycloak(readiness: ReadinessState, skip_checks: bool) -> bool:
    """Set up Keycloak realm and SSO. Returns True on success."""
    if skip_checks:
        readiness.keycloak.mark_ready()
        return True

    try:
        logger.info("Waiting for Keycloak to become available")
        await wait_for_keycloak_availability()

        logger.info("Setting up Keycloak (realm, SSO, scopes, and operations client)")
        keycloak_success = await setup_keycloak()
        if not keycloak_success:
            readiness.keycloak.mark_failed("Keycloak setup returned failure")
            return False

        logger.info("Ensuring operations manager has valid Keycloak credentials")
        credentials_success = await keycloak_client_exists_and_works()
        if not credentials_success:
            readiness.keycloak.mark_failed("Failed to ensure Keycloak credentials")
            return False

        readiness.keycloak.mark_ready()
        return True
    except Exception as e:
        readiness.keycloak.mark_failed(str(e))
        return False


async def _setup_oauth(readiness: ReadinessState, app: FastAPI) -> bool:
    """Register OAuth client. Returns True on success."""
    try:
        await register_oauth_client_after_keycloak_setup(app)
        readiness.oauth.mark_ready()
        return True
    except Exception as e:
        readiness.oauth.mark_failed(str(e))
        return False


STARTUP_RETRY_INTERVAL = 60


async def _startup_retry_loop(app: FastAPI, skip_checks: bool) -> None:
    """Background loop that retries failed startup phases every 60 seconds."""
    from opi.core.readiness import get_readiness_state

    readiness = get_readiness_state()

    while not readiness.is_ready:
        unavailable = [s.display_name for s in readiness.get_unavailable_services()]
        logger.info(
            f"Startup retry: waiting {STARTUP_RETRY_INTERVAL}s before retrying "
            f"unavailable services: {', '.join(unavailable)}"
        )
        await asyncio.sleep(STARTUP_RETRY_INTERVAL)

        if not readiness.database.ready:
            await _setup_database(readiness)

        if not readiness.projects.ready and readiness.database.ready:
            await _setup_projects(readiness, app, skip_checks)

        if not readiness.keycloak.ready:
            await _setup_keycloak(readiness, skip_checks)

        if not readiness.oauth.ready and readiness.keycloak.ready:
            await _setup_oauth(readiness, app)

    logger.info("All services are now ready - startup retry loop complete")


async def _resolve_running_image() -> None:
    """Ask the cluster which image this pod runs and hand it to ``/version``.

    Once, at startup: a pod's image cannot change while it runs, so a later lookup
    would answer the same thing at the cost of a kubectl call on a public endpoint.
    Outside Kubernetes (docker-compose, tests) there is no pod name and nothing to
    ask, and the field stays empty rather than guessing from an env var.
    """
    pod_name = os.environ.get("POD_NAME", "")
    namespace = os.environ.get("POD_NAMESPACE", "")
    if not pod_name or not namespace:
        logger.debug("No POD_NAME/POD_NAMESPACE in the environment; /version reports no image")
        return

    try:
        image = await create_kubectl_connector().get_pod_container_image(
            namespace=namespace, pod_name=pod_name, container_name=CONTAINER_NAME
        )
    except (KubectlConnectionError, KubectlExecutionError) as exc:
        # Not being able to read its own pod is not a reason to refuse to start.
        logger.warning("Could not resolve the running image for pod %s/%s: %s", namespace, pod_name, exc)
        return

    if image:
        set_running_image(image)


async def run_startup_tasks(app: FastAPI) -> bool:
    """
    Run all startup tasks for the application.

    Services that fail will be retried in a background loop every 60 seconds.
    The application starts immediately and serves a status page until all
    critical services are available.

    Returns:
        True if all startup tasks completed successfully on first attempt
    """
    from opi.core.readiness import get_readiness_state

    readiness = get_readiness_state()
    skip_checks = settings.SKIP_STARTUP_CHECKS

    if skip_checks:
        logger.warning("SKIP_STARTUP_CHECKS=True - skipping namespace/Keycloak/MinIO checks for fast startup")

    logger.info("Running startup tasks...")

    # Every schema version a project file can declare must have a schema to be
    # validated against. A migration added without one would otherwise show up as
    # project files being rejected by the git-monitor gate, months later and
    # without an obvious cause; this stops the boot instead.
    check_schema_versions(SCHEMA_VERSIONS)

    # Initialize metrics connector (non-critical)
    logger.info("Initializing metrics connector")
    metrics_connector = await get_metrics_connector()
    if not metrics_connector.is_connected:
        logger.warning("Metrics connector not available at startup, starting background reconnection task")
        app.state.metrics_reconnect_task = asyncio.create_task(start_prometheus_reconnection_task())

    # Phase 1: Database
    await _setup_database(readiness)

    # Phase 2: Project files (requires database)
    if readiness.database.ready:
        await _setup_projects(readiness, app, skip_checks)

    # Resolve which image this pod runs, so /version can say who is answering.
    await _resolve_running_image()

    # Phase 3: MinIO check (non-critical, no retry)
    if not skip_checks:
        await check_minio_availability()

    # Phase 3b: ZAD's own SMTP account (non-critical, no retry)
    if not skip_checks:
        await ensure_platform_mail_account()

    # Phase 4: Keycloak
    await _setup_keycloak(readiness, skip_checks)

    # Phase 5: OAuth (requires Keycloak)
    if readiness.keycloak.ready:
        await _setup_oauth(readiness, app)

    # Phase 6: CAA records on our own DNS zones (non-critical)
    try:
        await reconcile_caa_records()
    except Exception as e:  # non-critical: DNS hygiene must never block boot
        logger.error(f"CAA reconciliation failed: {e}")

    # Phase 7: no-mail records on the names we publish ourselves (non-critical)
    try:
        await reconcile_no_mail_records()
    except Exception as e:  # non-critical: DNS hygiene must never block boot
        logger.error(f"No-mail reconciliation failed: {e}")

    if readiness.is_ready:
        logger.info("All startup tasks completed successfully")
        return True

    # Start background retry loop for failed services
    unavailable = [s.display_name for s in readiness.get_unavailable_services()]
    logger.warning(
        f"Some services are unavailable: {', '.join(unavailable)}. "
        f"Starting background retry loop (every {STARTUP_RETRY_INTERVAL}s)."
    )
    app.state.startup_retry_task = asyncio.create_task(_startup_retry_loop(app, skip_checks))
    return False
