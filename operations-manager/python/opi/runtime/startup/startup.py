"""
Startup logic for the Operations Manager application.

This module handles startup tasks like ensuring namespaces exist from project files,
setting up shared SOPS keys, and other initialization tasks.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import aiohttp
import httpx

if TYPE_CHECKING:
    from fastapi import FastAPI

    from opi.connectors.kubectl import KubectlConnector
    from opi.core.readiness import ReadinessState
from keycloak.exceptions import KeycloakError

from opi.api.endpoint_util import controleer_handelingen
from opi.connectors.git_repo import warm_platform_repos
from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnectionError, KubectlExecutionError, create_kubectl_connector
from opi.connectors.mail import MailRelayError
from opi.connectors.minio_mc import create_minio_connector
from opi.connectors.prometheus import get_metrics_connector
from opi.core.authz.rules import check_register
from opi.core.cluster_config import get_prefixed_namespace, is_platform_namespace
from opi.core.config import settings
from opi.core.database_pools import initialize_database_pools
from opi.core.version import set_running_image
from opi.manager.bootstrap.keycloak_setup import setup_keycloak
from opi.manager.project_manager import ProjectManager
from opi.runtime.periodic.caa_reconciler import reconcile_caa_records
from opi.runtime.periodic.no_mail_reconciler import reconcile_no_mail_records
from opi.runtime.startup.keycloak_client_startup import ensure_keycloak_credentials
from opi.runtime.startup.platform_database_access import restrict_platform_databases
from opi.services.project_key_secret import CMP_SOPS_ROLE_NAME, SOPS_SECRET_NAME, ProjectKeySecretHandler
from opi.services.project_schema import check_schema_versions
from opi.services.project_service import initialize_project_service
from opi.services.project_store import get_project_store
from opi.services.schema_migration import SCHEMA_VERSIONS
from opi.services.session_revocation_service import load_revocations
from opi.services.user_service import get_user_service
from opi.utils.connect_retry import retry_on_connect_error
from opi.utils.git_signing import load_git_signing_identity

logger = logging.getLogger(__name__)

# Name of this application's container in its own pod (bootstrap/rig-system deployment).
CONTAINER_NAME = "operations-manager"

#: Bound on the startup round of the key reader: after this time the round stops and logs
#: what it did so far, so a slow or unreachable cluster does not run on forever.
CMP_KEY_READER_ROUND_TIMEOUT_SECONDS = 600


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


#: What a Keycloak that is not up yet answers with. The ordinary "not ready" is NOT in it:
#: ``connection_works`` swallows a KeycloakError into a False and the probe below then
#: raises RuntimeError, which is therefore not retried. Measured and left as it was in
#: tests/test_keycloak_retry_characterisation.py.
_KEYCLOAK_UNAVAILABLE_ERRORS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.HTTPStatusError,
    KeycloakError,
    OSError,
)

#: Six minutes, enough for a Keycloak that is still starting on a fresh cluster.
_KEYCLOAK_WAIT_BUDGET_SECONDS = 360.0


async def _keycloak_answers() -> None:
    """Raises unless Keycloak answers over client credentials or over the admin password."""
    logger.info("Checking Keycloak availability...")

    # Prefer OPI's service account; on a fresh cluster that client does not exist yet (it
    # is created later in setup_keycloak), so fall back to the admin password.
    keycloak = await create_keycloak_connector()
    if await keycloak.connection_works():
        logger.info("Keycloak is available and responding")
        return

    admin = await create_keycloak_connector(use_client_credentials=False)
    if await admin.connection_works():
        logger.info("Keycloak is available and responding (admin-password fallback)")
        return

    raise RuntimeError("Keycloak did not respond via client-credentials or admin password")


async def wait_for_keycloak_availability() -> bool:
    """Waits for a Keycloak that is not ready yet during application startup.

    Raises when it is still not available after the budget is spent.
    """
    await retry_on_connect_error(
        _keycloak_answers,
        _KEYCLOAK_WAIT_BUDGET_SECONDS,
        "Keycloak availability",
        retry_on=_KEYCLOAK_UNAVAILABLE_ERRORS,
    )
    return True


def print_boot_banner():
    """Print a distinctive boot banner for easy log identification."""
    import datetime

    boot_time = datetime.datetime.now(tz=datetime.UTC).isoformat()

    banner = f"""
{"=" * 80}
OPERATIONS MANAGER STARTING UP
{"=" * 80}
Boot Time: {boot_time}
Environment: {os.environ.get("ENVIRONMENT", "production")}
Debug Mode: {os.environ.get("DEBUG", "false")}
Git Monitoring: {os.environ.get("ENABLE_GIT_MONITOR", "false")}
{"=" * 80}
"""

    # Print to both stdout and logger
    print(banner)
    for line in banner.strip().split("\n"):
        if line.strip():
            logger.info(line)


# Nothing here keeps the project data fresh: reads come from the ProjectStore, whose cache is
# write-through for everything ZAD writes. An edit made outside ZAD (by hand, or from another
# cluster) is picked up by store.reconcile(), called by the refresh action and by the fallback
# poll start_reconcile_poll in project_store. Both start with an ls-remote check, so an idle
# tick costs nothing.


def _cluster_deployment_namespaces() -> dict[str, str]:
    """The deployment namespaces of this cluster, with the project name per namespace."""
    namespaces: dict[str, str] = {}
    for project in get_project_store().get_all():
        data = project.data or {}
        for deployment in data.get("deployments", []) or []:
            if deployment.get("cluster") != settings.CLUSTER_MANAGER:
                continue
            namespace = deployment.get("namespace")
            if not namespace:
                continue
            prefixed = get_prefixed_namespace(settings.CLUSTER_MANAGER, namespace)
            # The declared namespace is tenant input and is not under enforce_namespace_pin
            # here: that one only runs during processing. A project declaring 'operations'
            # would otherwise land on the platform namespace, where 'sops-age-key' is the
            # platform key.
            if is_platform_namespace(prefixed):
                logger.warning(
                    f"Project {project.name} declares platform namespace {prefixed}; "
                    f"skipped in the CMP key reader round"
                )
                continue
            namespaces[prefixed] = project.name
    return namespaces


async def _ensure_cmp_key_reader_in_namespace(
    namespace: str,
    project_name: str,
    kubectl: KubectlConnector,
    sops_handler: ProjectKeySecretHandler,
    counts: dict[str, int],
) -> None:
    """Put the key reader in one namespace and update the counts."""
    role_present = await kubectl.resource_exists("role", CMP_SOPS_ROLE_NAME, namespace)
    if role_present is None:
        counts["gefaald"] += 1
        logger.error(f"Could not read the CMP key reader role in namespace {namespace} (project {project_name})")
        return
    if role_present:
        counts["aanwezig"] += 1
        logger.debug(f"CMP key reader role already present in namespace {namespace}")
        return

    secret_present = await kubectl.secret_exists(SOPS_SECRET_NAME, namespace)
    if secret_present is None:
        counts["gefaald"] += 1
        logger.error(f"Could not read secret {SOPS_SECRET_NAME} in namespace {namespace} (project {project_name})")
        return
    if not secret_present:
        if not await kubectl.namespace_exists(namespace):
            counts["gefaald"] += 1
            logger.error(f"Namespace {namespace} of project {project_name} does not exist on this cluster")
            return
        counts["overgeslagen"] += 1
        logger.info(f"No {SOPS_SECRET_NAME} in namespace {namespace} yet, so no CMP key reader role either")
        return

    try:
        await sops_handler.ensure_cmp_key_reader(namespace)
    except (KubectlExecutionError, KubectlConnectionError) as e:
        counts["gefaald"] += 1
        logger.error(f"Failed to apply the CMP key reader role in namespace {namespace}: {e}")
        return
    counts["gezet"] += 1
    logger.info(f"Applied the missing CMP key reader role in namespace {namespace} (project {project_name})")


async def ensure_cmp_key_readers() -> dict[str, int]:
    """Give every project namespace of this cluster with a key secret the CMP key reader.

    Projects from before RC-303 have the secret but not the Role, and without that Role
    ArgoCD no longer renders them. This round repairs that without a project having to be
    processed again.

    Next to the project round of `_setup_projects`, which takes the Role along per project
    but skips a project as soon as an earlier step in it fails.

    Returns the counts, keyed ``gezet``, ``aanwezig``, ``overgeslagen`` and ``gefaald``.
    """
    kubectl = create_kubectl_connector()
    sops_handler = ProjectKeySecretHandler(kubectl)
    counts = {"gezet": 0, "aanwezig": 0, "overgeslagen": 0, "gefaald": 0}

    namespaces = _cluster_deployment_namespaces()
    logger.info(f"Checking the CMP key reader role in {len(namespaces)} project namespaces")

    try:
        async with asyncio.timeout(CMP_KEY_READER_ROUND_TIMEOUT_SECONDS):
            for namespace in sorted(namespaces):
                # A kubectl that drops away may not end the round without a summary: the
                # release checks for exactly that one line.
                try:
                    await _ensure_cmp_key_reader_in_namespace(
                        namespace, namespaces[namespace], kubectl, sops_handler, counts
                    )
                except (KubectlExecutionError, KubectlConnectionError) as e:
                    counts["gefaald"] += 1
                    logger.error(f"CMP key reader check failed in namespace {namespace}: {e}")
    except TimeoutError:
        logger.error(
            f"CMP key reader round stopped after {CMP_KEY_READER_ROUND_TIMEOUT_SECONDS}s, "
            f"{len(namespaces)} namespaces were in scope"
        )

    logger.info(
        f"CMP key reader round: {counts['gezet']} applied, {counts['aanwezig']} already present, "
        f"{counts['overgeslagen']} without a key secret, {counts['gefaald']} failed"
    )
    return counts


async def register_oauth_client_after_keycloak_setup(app) -> None:
    """Call this once the Keycloak credentials are available, so the OAuth client is
    registered for authentication.

    Raises ``RuntimeError`` when registering the OAuth client fails.
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
    logger.info(f"  - client_secret: {'set' if settings.OIDC_CLIENT_SECRET else 'not set'}")
    logger.info(f"  - discovery_url: {settings.OIDC_DISCOVERY_URL}")

    oauth.register(  # type: ignore
        name="keycloak",
        client_id=settings.OIDC_CLIENT_ID,
        client_secret=settings.OIDC_CLIENT_SECRET,
        server_metadata_url=settings.OIDC_DISCOVERY_URL,
        client_kwargs={
            "scope": "openid profile email",
            # PKCE with S256, required by the OPI client in Keycloak.
            "code_challenge_method": "S256",
        },
    )


async def keycloak_client_exists_and_works() -> bool:
    """Delegates to the credential management that creates or retrieves the credentials when
    they are missing or invalid.
    """
    return await ensure_keycloak_credentials()


_MAILACCOUNT_START_ERRORS = (
    MailRelayError,
    KubectlExecutionError,
    KubectlConnectionError,
    ValueError,
    aiohttp.ClientError,
    OSError,
)
"""What a mail account may fail on at startup without taking the boot down with it.

Both mail accounts catch this list, and it lives here ONCE because the two functions have
the same reason: ``server.py`` awaits ``run_startup_tasks`` without a guard, so an escaping
error takes phases 4 and 5 with it. The shapes are explained in
``ensure_platform_mail_account`` below.
"""


async def ensure_platform_mail_account() -> bool | None:
    """Make sure ZAD's own account exists on the mail relay.

    Three outcomes, and the difference between the last two is what the retry loop needs:
    ``True`` when the account is there, ``False`` when this can still succeed (the relay is
    not up yet, the Secret was briefly unreadable), and ``None`` when there is nothing to set
    up. The same three as ``MailManager.ensure_platform_account`` below it.

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
    unreachable arrives as ``MailRelayUnavailableError`` (the connector turns aiohttp's own
    transport error into one), but the raw transport errors stay in the list:
    they still reach here from the kubectl call and the decryption around it, which is what
    ``check_minio_availability`` right below catches too. Same for the kubectl failure from
    writing the account's Secret (a cluster where that is refused must still boot).

    That last one has two shapes, and both are normal on a fresh cluster: kubectl raises
    ``KubectlConnectionError`` -- NOT a subclass of ``KubectlExecutionError`` -- when the
    API server is unreachable, which is exactly the state its own retry loop exists for.
    And ``create_mail_connector`` decrypts ``MAIL_RELAY_ADMIN_PASSWORD``, so an admin
    password that is not (yet) decryptable arrives here as a plain ``ValueError``; the
    first boot after someone sets ``MAIL_RELAY_API_URL`` is the likely moment for it.
    """
    from opi.manager.mail_manager import MailManager

    try:
        account = await MailManager.ensure_platform_account()
    except _MAILACCOUNT_START_ERRORS as error:
        logger.error(f"Platform-mailaccount kon niet worden ingericht: {error}")
        return False
    if account is None:
        return None
    logger.info(f"Platform-mailaccount {account.username} staat klaar ({account.from_address})")
    return True


async def ensure_keycloak_mail_account() -> bool | None:
    """Make sure KEYCLOAK's account exists on the mail relay.

    The same three outcomes as ``ensure_platform_mail_account``: ``None`` means there is
    nothing to set up on this cluster, and that is not a failure to retry.

    Next to ``ensure_platform_mail_account`` and not inside it: the two accounts differ in
    where the password comes from (OPI generates the one, the bootstrap makes the other),
    and a cluster can perfectly well have one without the other -- an older cluster has the
    platform account and not yet the Secret this one reads.

    Non-critical, and for a sharper reason than the platform account: what this blocks is
    every realm's login mail, and a boot that fails over it would take the portal down
    together with the mail. So the same catch list applies, and for the same reason -- an
    exception escaping here takes ``run_startup_tasks`` and therefore phases 4 and 5 with
    it.

    Two shapes are NOT errors and answer ``None`` without a word of alarm: no relay on
    this cluster, and no Secret yet. Both are logged by the manager.
    """
    from opi.manager.mail_manager import MailManager

    try:
        account = await MailManager.ensure_keycloak_account()
    except _MAILACCOUNT_START_ERRORS as error:
        logger.error(f"Keycloak-mailaccount kon niet worden ingericht: {error}")
        return False
    if account is None:
        return None
    logger.info(f"Keycloak-mailaccount {account.username} staat klaar ({account.from_address})")
    return True


async def check_minio_availability() -> bool:
    """Whether the ``mc`` CLI the MinIO connector needs is installed and functional."""
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

        # Which sessions are revoked, in memory: the per-request check reads there
        # (opi/core/session_guard.py). If this goes wrong, that check is quietly off, so the
        # error should stop the startup.
        await load_revocations()

        if settings.ADMIN_EMAILS:
            env_admin_emails = [email.strip() for email in settings.ADMIN_EMAILS.split(",") if email.strip()]
            if env_admin_emails:
                get_user_service().add_platform_admins(env_admin_emails)

        # Loading every project into the cache is the store's job, and only the store's: a
        # second loader here is a second chance for the cache to disagree with git.
        store = get_project_store()
        await store.bootstrap()

        # Provisioning is a separate concern from loading: it needs the project list, not
        # the file walk, so it runs off the cache the store just populated.
        if not skip_checks:
            # Read the cluster's namespaces once instead of per project-deployment: a
            # kubectl fork per deployment dominated the boot time.
            known_namespace_labels: dict[str, dict[str, str]] | None = None
            try:
                known_namespace_labels = await create_kubectl_connector().get_namespace_labels_map()
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

#: What the mail accounts are called in the retry list. They are not in ``ReadinessState``:
#: that decides whether the portal shows its maintenance page, and without platform mail the
#: portal should simply work.
MAIL_ACCOUNTS_NAME = "Platform mail accounts"


async def _setup_mail_accounts(skip_checks: bool) -> bool:
    """Set up the two platform mail accounts. Answers whether they are dealt with.

    ``False`` means "not done yet, and a next attempt can still get there": on a fresh
    rollout the relay is usually not up when OPI starts, and ``zad-platform`` and
    ``zad-keycloak`` then only came into being on the next restart of OPI, and without those
    accounts no platform mail and no login mail goes out.

    A cluster without a relay or without a Keycloak secret is dealt with (``True``): there is
    nothing to set up there, and a loop that keeps ticking on it writes a line every minute
    without ever ending.
    """
    if skip_checks:
        return True
    platform = await ensure_platform_mail_account()
    keycloak = await ensure_keycloak_mail_account()
    return platform is not False and keycloak is not False


async def _startup_retry_loop(app: FastAPI, skip_checks: bool, mail_accounts_done: bool = True) -> None:
    """Background loop that retries failed startup phases every 60 seconds."""
    from opi.core.readiness import get_readiness_state

    readiness = get_readiness_state()

    while not readiness.is_ready or not mail_accounts_done:
        unavailable = [s.display_name for s in readiness.get_unavailable_services()]
        if not mail_accounts_done:
            unavailable.append(MAIL_ACCOUNTS_NAME)
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

        if not mail_accounts_done:
            mail_accounts_done = await _setup_mail_accounts(skip_checks)

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


async def warm_connector_singletons() -> None:
    """Build the CLI connector singletons up front, in a thread.

    All four do a one-off ``subprocess.run(..., timeout=10)`` in their constructor to see
    whether their binary is there. That construction otherwise fell in the middle of the
    first task that needed the connector, synchronously on the event loop.
    """
    from opi.connectors.kopia import create_kopia_connector
    from opi.connectors.kubectl import create_kubectl_connector
    from opi.connectors.minio_mc import create_minio_connector
    from opi.connectors.skopeo import SkopeoConnector

    def _build() -> None:
        for make in (create_kubectl_connector, create_minio_connector, create_kopia_connector, SkopeoConnector):
            try:
                make()
            except Exception as e:
                # A missing binary is no reason to abandon the startup: the connectors set
                # their own availability flag and fall back.
                logger.warning(f"Kon {make.__name__} niet vooraf bouwen: {e}")

    await asyncio.to_thread(_build)


async def run_startup_tasks(app: FastAPI) -> bool:
    """Run all startup tasks for the application.

    Services that fail are retried in a background loop every 60 seconds. The application
    starts immediately and serves a status page until all critical services are available.

    Returns True only when every startup task succeeded on the FIRST attempt.
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

    # Without a key OPI commits as it always did, with only a WARNING. A key gpg cannot
    # import is a configuration error and stops the startup.
    load_git_signing_identity()

    # A guarded route without an action would quietly open everything the caller was already
    # allowed, and a role without a single grant opens nothing without anyone noticing. That
    # stops the startup here, not a 403 that fails to appear.
    controleer_handelingen(app.routes)
    check_register()

    # Metrics are not critical and the boot never waits for them: the connector starts
    # out unavailable and probes the backend in the background, so a Grafana that is down
    # cannot hold up the startup. It keeps probing with backoff until it answers.
    logger.info("Starting the metrics availability probe")
    metrics_connector = await get_metrics_connector()
    metrics_connector.start_probe()

    # Phase 1: Database
    await _setup_database(readiness)

    # Phase 2: Project files (requires database)
    if readiness.database.ready:
        await _setup_projects(readiness, app, skip_checks)

    # Phase 2b: the platform databases for their own role only
    await restrict_platform_databases()

    # Phase 2c: the CMP key reader in existing namespaces, in the background so the
    # readiness does not wait on it.
    if readiness.projects.ready and not skip_checks:
        app.state.cmp_key_reader_task = asyncio.create_task(ensure_cmp_key_readers())

    # Phase 2d: warm up bare copies of deployments and argo repos in the background.
    # A clone costs minutes; without this the first writing task pays for it.
    app.state.git_warm_task = asyncio.create_task(warm_platform_repos())

    # Resolve which image this pod runs, so /version can say who is answering.
    await _resolve_running_image()

    # Phase 3: MinIO check (non-critical, no retry)
    if not skip_checks:
        await check_minio_availability()

    # Phase 3b: the platform's SMTP accounts (non-critical; retried by the loop below)
    mail_accounts_done = await _setup_mail_accounts(skip_checks)

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

    if readiness.is_ready and mail_accounts_done:
        logger.info("All startup tasks completed successfully")
        return True

    # Start background retry loop for failed services
    unavailable = [s.display_name for s in readiness.get_unavailable_services()]
    if not mail_accounts_done:
        unavailable.append(MAIL_ACCOUNTS_NAME)
    logger.warning(
        f"Some services are unavailable: {', '.join(unavailable)}. "
        f"Starting background retry loop (every {STARTUP_RETRY_INTERVAL}s)."
    )
    app.state.startup_retry_task = asyncio.create_task(_startup_retry_loop(app, skip_checks, mail_accounts_done))
    return False
