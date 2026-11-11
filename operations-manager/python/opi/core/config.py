from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

# Initialize logging early to ensure it's available during config loading
from opi.core.early_logging import initialize_logging  # noqa: F401 (side-effect import)
from opi.core.logging_config import setup_logging
from opi.core.secret_key import generate_secret_key, validate_secret_key

logger = logging.getLogger(__name__)

PROJECT_NAME: str = "OPI"
VERSION: str = os.environ.get("ZAD_VERSION", "0.1.0")
BUILD_DATE: str = os.environ.get("ZAD_BUILD_DATE", "")
PROJECT_DESCRIPTION: str = "OPI - Operational Platform Interface"

#: The only cluster that may be a target of the attack proof.
PROOF_TARGET_CLUSTER = "sandboxed-local"

#: The header a proof target bounces the token of the running proof back in, on /health.
SECURITY_PROOF_HEADER = "X-Zad-Proef"


def _check_env_file_for_environment_var(file_path: str) -> None:
    """Warns when an .env file sets ENVIRONMENT itself."""
    try:
        with open(file_path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue

                # Check if line defines ENVIRONMENT variable
                if line.startswith(("ENVIRONMENT=", "ENVIRONMENT ")):
                    environment_value = line.split("=", 1)[1] if "=" in line else ""
                    logger.warning(f"ENVIRONMENT variable found in {file_path}:{line_num}")
                    logger.warning(f"Value '{environment_value}' in {file_path} is IGNORED")
                    logger.warning("ENVIRONMENT is read from system environment variable only")
                    logger.warning(f"Remove 'ENVIRONMENT={environment_value}' from {file_path}")
                    break
    except Exception as e:
        logger.debug(f"Could not check {file_path} for ENVIRONMENT variable: {e}")


# Cache for env files to avoid multiple calls and duplicate logging
_env_files_cache: list[str] | None = None


def _get_env_files() -> list[str]:
    """The environment files to load, in order of precedence, and only the ones that exist.

    Configuration hierarchy (container env vars take highest precedence):
    1. Container environment variables (from Kubernetes secrets) - HIGHEST PRECEDENCE
    2. ConfigMap mounted .env file (container/Kubernetes overrides)
    3. .env.{ENVIRONMENT} (environment-specific files, only for environments in ENVIRONMENT list)
    4. .env (base configuration file - always loaded) - LOWEST PRECEDENCE

    ENVIRONMENT is read from the system environment variable first to avoid a circular
    dependency. It can be a single value or a comma-separated list (e.g. "production,kubernetes").
    If it is not set in the system env it defaults to 'production' (fail closed).

    Examples:
    - ENVIRONMENT="local" -> loads .env, then .env.local
    - ENVIRONMENT="production" -> loads .env, then .env.production
    - ENVIRONMENT="production,kubernetes" -> loads .env, then .env.production, then .env.kubernetes
    """
    global _env_files_cache

    # Return cached result to avoid duplicate logging and processing
    if _env_files_cache is not None:
        return _env_files_cache
    env_files = []

    # Get ENVIRONMENT from system environment variable first (not from .env files to avoid circular dependency)
    environment_var = os.environ.get("ENVIRONMENT", "production")
    environments = [env.strip() for env in environment_var.split(",")]
    # Logging is now initialized via early_logging import
    logger.debug(f"Using ENVIRONMENT={environment_var} -> environments={environments}")

    # 1. Base .env file (should always exist in development)
    base_env = ".env"
    if os.path.exists(base_env):
        env_files.append(base_env)
        logger.debug(f"Found base env file: {base_env}")
        _check_env_file_for_environment_var(base_env)

    # 2. Environment-specific files (.env.production, .env.kubernetes, .env.local, etc.)
    for environment in environments:
        env_specific = f".env.{environment}"
        if os.path.exists(env_specific):
            env_files.append(env_specific)
            logger.debug(f"Found environment-specific env file: {env_specific}")
            _check_env_file_for_environment_var(env_specific)
        else:
            # Environment-specific files are optional. In production, config comes from
            # the ConfigMap mount + env vars + SOPS secrets, so .env.<environment> is
            # expected to be absent there - this is not an error (matches the DEBUG
            # level used above when the file IS found).
            logger.debug(f"No environment-specific file .env.{environment} (optional)")

    # 3. ConfigMap mounted environment file (for Kubernetes deployments)
    # Check multiple possible mount paths
    configmap_paths = [
        "/etc/config/.env",  # Standard ConfigMap mount path
        "/app/config/.env",  # Alternative app-specific mount path
        "/config/.env",  # Simple config mount path
        os.environ.get("CONFIG_ENV_FILE_PATH", ""),  # Configurable via env var
    ]

    configmap_found = False
    for configmap_path in configmap_paths:
        if configmap_path:  # Skip empty paths
            if os.path.exists(configmap_path):
                env_files.append(configmap_path)
                logger.info(f"ConfigMap env file found and loaded: {configmap_path}")
                configmap_found = True
                break  # Only use the first ConfigMap file found
            else:
                logger.debug(f"ConfigMap env file not found at: {configmap_path}")

    if not configmap_found:
        logger.info("No ConfigMap env file found - running with base configuration only")

    logger.info(f"Configuration loading order: {env_files}")

    # Cache the result to avoid duplicate processing and logging
    _env_files_cache = env_files
    return env_files


class Settings(BaseSettings):
    # ``extra="allow"`` instead of pydantic-settings' default ``forbid``: an unknown key is
    # reported as a warning naming it (``_warn_about_unknown_settings``), not fatal. So a
    # typo still surfaces, while an image that meets a config file mentioning a newer
    # setting than itself (a rollback, an upgrade test) still starts.
    model_config = {"env_file": _get_env_files(), "env_file_encoding": "utf-8", "extra": "allow"}

    def model_post_init(self, __context: Any) -> None:
        self._warn_about_unknown_settings()

    def _warn_about_unknown_settings(self) -> None:
        """Name every config key this build does not know.

        Two causes, and the message cannot tell them apart: a typo, or a setting from a
        newer version than this image. Both are worth seeing.
        """
        unknown = sorted(self.model_extra or {})
        if unknown:
            logger.warning(
                f"{len(unknown)} unknown configuration key(s) ignored by this build "
                f"(a typo, or a setting newer than this image): {', '.join(unknown)}"
            )

    OWN_DOMAIN: str = "operations-manager.kind"
    ADDITIONAL_DOMAINS: str = ""  # Comma-separated list of additional domains for redirect URIs

    SECRET_KEY: str = Field(default_factory=generate_secret_key)
    # Default "production": a forgotten setting falls to the closed side, without proof and
    # test routes. Local, sandbox and the dev compose set their value explicitly.
    ENVIRONMENT: str = "production"
    DEBUG: bool = False
    CLUSTER_MANAGER: str = "local"

    # The OIN in ``notification_events.source``. Until it is issued this is a placeholder, so
    # rows are CloudEvents-shaped but not valid; exporters have to refuse this.
    EVENT_SOURCE_OIN: str = "oin-nog-niet-toegekend"

    # Idle session timeout. The session cookie is re-signed on every response that
    # mutates the session, and the server-side row renews on activity via touch: a
    # session expires only after this many seconds of inactivity.
    SESSION_MAX_AGE_SECONDS: int = 28800  # 8 hours - one workday

    # Hot cache for the server-side sessions (RC-390): an LRU with a maximum plus a per-item
    # TTL. After the TTL the database is the source again, so a second pod never lags further.
    SESSION_CACHE_MAX_ENTRIES: int = 5000
    SESSION_CACHE_TTL_SECONDS: float = 60.0

    # Absolute maximum duration of a session from the login on, however active (BIO2 8.05,
    # ASVS 3.3.1). The inactivity bound above is a sliding window; this one is hard.
    SESSION_ABSOLUTE_MAX_SECONDS: int = 43200  # 12 hours

    # How long one step-up code counts. The same window opens platform admin rights.
    STEP_UP_WINDOW_MINUTES: int = 5

    # Failed authentication attempts per minute on the key paths, counted separately per
    # client and per offered key. Above the bound a 429 follows. See
    # features/een-foute-sleutel-loopt-tegen-een-snelle-weigering.md for which of the two
    # refuses BEFORE the key comparison and why the other one sits after it.
    AUTH_FAILURE_LIMIT_PER_MINUTE: int = 10

    # The coarse upper bound on /api and /auth per client. Generous: it should flatten the
    # peak, not touch ordinary use.
    API_RATE_LIMIT_PER_MINUTE: int = 600
    API_RATE_LIMIT_BURST: int = 120

    # Ceiling on the Retry-After that goes along with a 429.
    RATE_LIMIT_RETRY_AFTER_MAX_SECONDS: int = 60

    # Number of trusted proxy hops in X-Forwarded-For, counted from the RIGHT. 0 trusts no
    # header at all and counts on the TCP peer address: the right value for a setup without a
    # proxy in front of it (docker-compose, pytest, uv run). Every setup with an ingress in
    # front sets it to 1 in its own configmap, because that one proxy is the only entrance
    # there. See features/futures/client-ip-vertrouwen-achter-de-router.md.
    TRUSTED_PROXY_HOPS: int = 0

    # The per-run minted token the attack proof recognises this instance as a target by
    # (header X-Zad-Proef on /health).
    SECURITY_PROOF_TOKEN: str = ""

    # Development mode: "reload" (hot-reload) | "debug" (debugger) | "production"
    # Default "production": a forgotten setting falls to the closed side, without file
    # watching and without a debugger. Local and sandbox set "reload" explicitly.
    DEBUG_MODE: str = "production"

    # Developer settings
    FIXED_PROJECT_POSTFIX: str | None = None  # If set, use this instead of random postfix for project names
    ALLOW_PROJECTFILES_OVERWRITE: bool = False  # If True, allow overwriting existing project files
    RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE: bool = (
        False  # If True, recreate passwords/users when authentication fails
    )
    SKIP_STARTUP_CHECKS: bool = False  # Skip namespace/Keycloak/MinIO checks on startup (for fast local dev)

    # OIDC settings
    OIDC_CLIENT_ID: str | None = None
    OIDC_CLIENT_SECRET: str | None = None
    OIDC_DISCOVERY_URL: str | None = None

    # CLI settings - the public OIDC client the zad-cli uses for the
    # authorization-code + PKCE loopback flow (RFC 8252), and the audience its
    # access tokens must carry for this API to accept them.
    CLI_CLIENT_ID: str = "zad-cli"
    CLI_TOKEN_AUDIENCE: str = "zad-api"  # noqa: S105  # the audience of the CLI token, not a secret

    # How long an API token may be valid at most, in days. Platform policy and not a choice
    # of whoever creates the token: without a maximum everyone picks the maximum.
    TOKEN_MAX_DAGEN_PERSOONLIJK: int = 90
    TOKEN_MAX_DAGEN_SERVICE_ACCOUNT: int = 365

    # Invite system settings
    INVITE_CLIENT_ID: str = "operations-manager-invites"

    # Access control settings
    ALLOWED_EMAILS: str | None = None  # Comma-separated list of allowed email addresses
    ADMIN_EMAILS: str | None = None  # Comma-separated list of admin emails (can view all projects)

    # Git projects server settings - the repository that holds the project files
    GIT_PROJECTS_SERVER_URL: str = "git://localhost:9090/"
    GIT_PROJECTS_SERVER_USERNAME: str | None = None  # Username for Git projects server authentication
    GIT_PROJECTS_SERVER_PASSWORD: str | None = None  # Password for Git projects server (can be SOPS encrypted)
    # Web address of the projects repo, for commit links in the audit tab.
    GIT_PROJECTS_WEB_URL: str = ""
    GIT_PROJECTS_SERVER_REPO_PATH: str = "/"
    GIT_PROJECTS_SERVER_BRANCH: str = "main"
    # Fallback poll for edits made outside ZAD (by hand, or from another cluster).
    # Bounds how long an out-of-band revocation (member removed, invite key revoked
    # by pushing to zad-projects directly) can keep working on this instance.
    # Cheap when idle: reconcile() starts with an ls-remote check. 0 disables.
    PROJECT_STORE_RECONCILE_INTERVAL_SECONDS: int = 300

    # Project deployment repository - shared repo where all project manifests are pushed
    # This is a single shared repository; projects are separated by subdirectories internally
    PROJECT_REPO_URL: str = "https://github.com/RijksICTGilde/rig-cluster-application-test.git"
    PROJECT_REPO_USERNAME: str = "git"
    # The PAT for this repository belongs in the OPI env-secret (per cluster), never as a
    # default in the image: a password baked into code rotates on image cadence while the
    # matching secret rotates on the platform cadence, and the two then silently differ. The
    # sandbox gets it through the overlay configmap; odcn through
    # operations-manager-env-secrets.
    PROJECT_REPO_PASSWORD: str | None = None
    PROJECT_REPO_BRANCH: str = "main"

    # Comma-separated list of URL prefixes of helmfile and chart sources OPI clones. See
    # features/helmfile-single-app-deployment.md for the details.
    RENDER_SOURCE_ALLOWLIST: str = ""

    # ArgoCD Applications Git repository - simplified to just URL and credentials
    GIT_ARGO_APPLICATIONS_URL: str = "ssh://git@localhost:2222/srv/git/argo-applications.git"
    GIT_ARGO_APPLICATIONS_PASSWORD: str | None = None
    GIT_ARGO_APPLICATIONS_BRANCH: str = "main"
    GIT_ARGO_APPLICATIONS_USERNAME: str | None = None

    ARGOCD_MANAGER: str = "rig-system"

    # ArgoCD Server Configuration
    ARGOCD_HOST: str = "argocd-server"
    ARGOCD_PORT: int = 80
    # No default; a forgotten setting has to be an error, not admin/admin.
    ARGOCD_USERNAME: str | None = None
    ARGOCD_PASSWORD: str | None = None
    # API token of the account `opi` (Secret opi-argocd-token). When it is set, the connector
    # skips /api/v1/session and sends this token as a bearer.
    ARGOCD_TOKEN: str | None = None
    ARGOCD_USE_TLS: bool = False
    ARGOCD_VERIFY_SSL: bool = False
    # Path of the mounted CA bundle of the ArgoCD server. Required once TLS and certificate
    # checking are on: the CA behind it differs per cluster.
    ARGOCD_CA_BUNDLE: str | None = None

    # Manifests path
    MANIFESTS_PATH: str = "manifests"

    # API security
    API_TOKEN: str = "d68d6aebd694d636e5eb4784a952b9c3"  # Example hardcoded token for development  # noqa: S105  # a dev default, overridden by the environment in a cluster
    USE_UNSAFE_API_KEY: bool = False  # Use hardcoded "secret" API key for development (set to True in .env.local)

    # SOPS age key settings (from Kubernetes secret)
    SOPS_AGE_KEY_FILE: str | None = None  # Path to the mounted platform key in the pod
    SOPS_AGE_KEY_CONTENT: str | None = None  # Fallback for local development, see load_sops_age_key_content
    SOPS_AGE_PUBLIC_KEY: str | None = None  # Public key for SOPS age encryption
    SOPS_AGE_PRIVATE_KEY: str | None = None  # Private key for SOPS age decryption

    # The project KEK: the key only the project keys are wrapped with. It deliberately sits
    # in the OPI pod only, see docs/development/sops-sleutel-in-het-cluster.md.
    PROJECT_KEK_FILE: str | None = None  # Path to the mounted KEK in the pod
    PROJECT_KEK_PUBLIC_KEY: str | None = None
    PROJECT_KEK_PRIVATE_KEY: str | None = None

    # OpenTelemetry configuration
    OTEL_ENABLED: bool = False  # Safe default - zero overhead when off
    OTEL_SERVICE_NAME: str = "opi-operations-manager"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://jaeger.rig-system:4317"
    OTEL_TRACES_SAMPLER_ARG: str = "1.0"  # 1.0 = 100% sampling for dev
    OTEL_LOG_CORRELATION: bool = True

    # Prometheus metrics configuration
    ENABLE_TRACEMALLOC: bool = False  # Enable tracemalloc for Python memory allocation tracking (adds ~10-30% overhead)

    # From this event loop lag on, the watchdog logs a WARNING with the stack of the main
    # thread. See features/event-loop-lag-watchdog.md.
    EVENT_LOOP_LAG_WARNING_SECONDS: float = 1.0

    # Logging configuration
    LOG_LEVEL: str = "DEBUG"  # Level of the opi logger; production stands on INFO
    LOG_TO_FILE: bool = False  # Enable file logging alongside stdout
    LOG_FILE_PATH: str = "log.txt"  # Path to log file when LOG_TO_FILE is enabled
    LOG_ERRORS_TO_FILE: bool = False  # Write WARNING+ to a separate persistent error log
    LOG_ERRORS_FILE_PATH: str = "/data/logs/errors.log"  # Path to error log (should be on PVC)

    # Temporary directory configuration
    TEMP_DIR: str = "/tmp"  # Default temp directory, can be overridden by TMPDIR env var
    # Temporary files with plain secrets. A tmpfs in the pod, so plain SOPS content does not
    # land on the node's disk as it would in TEMP_DIR.
    SECRET_TEMP_DIR: str = "/tmp"  # noqa: S105  # a path to a directory, not a secret

    # GPG private key (ASCII-armored, without a passphrase) for commits in the GitOps repos.
    # See features/opi-ondertekent-zijn-commits.md.
    GIT_SIGNING_KEY_FILE: str | None = None

    # Container registry configuration (for image upload proxy)
    REGISTRY_URL: str = ""  # Target registry (e.g., rcr.rijksapps.nl)
    REGISTRY_ORG: str = ""  # Organization prefix (e.g., rig)
    REGISTRY_USERNAME: str = ""  # Robot account name
    REGISTRY_PASSWORD: str = ""  # Robot token (supports age:/base64+age:/plain: prefixes)
    REGISTRY_VERIFY_TLS: bool = True
    IMAGE_UPLOAD_MAX_SIZE_MB: int = 5120  # Safety cap (5 GB)

    # Uploads. The functional ceiling per route sits elsewhere (MAX_ATTACHMENT_BYTES for an
    # attachment, IMAGE_UPLOAD_MAX_SIZE_MB above); this bounds what the pod carries of it.
    MAX_CONCURRENT_UPLOADS: int = 4  # concurrent uploads over all upload routes together
    UPLOAD_QUEUE_SIZE: int = 10  # uploads that may wait for a slot; above it, a 503 at once
    UPLOAD_QUEUE_WAIT_SECONDS: float = 30.0  # how long an upload may stand in the queue
    UPLOAD_SLOT_RETRY_AFTER_SECONDS: int = 5  # what that 503 hands over as Retry-After
    UPLOAD_SPILL_AFTER_BYTES: int = 1024 * 1024  # above this an upload goes to a temp file
    # The body bound of an attachment route is MAX_ATTACHMENT_BYTES times this number: the
    # multipart framing comes on top of the file, and 64 KB leaves little room for it.
    ATTACHMENT_BODY_LIMIT_FACTOR: int = 16

    # Keycloak configuration
    KEYCLOAK_URL: str = "https://keycloak.kind"
    KEYCLOAK_ADMIN_USERNAME: str = "admin"
    KEYCLOAK_ADMIN_PASSWORD: str = "changeMe123!"  # noqa: S105  # a dev default, overridden by the environment in a cluster

    # OPI's own client-credentials service account for Keycloak admin operations.
    # When the secret is set, OPI authenticates with this confidential master
    # client instead of the shared admin password, so the admin account can be
    # OTP-enforced/locked without locking OPI out. The admin password is only
    # needed for first-boot self-bootstrap of this client (and as break-glass).
    # A second, human master admin that carries an OTP credential from creation. Keycloak
    # makes the shared KEYCLOAK_ADMIN itself at first boot, so that one can never be born
    # with a second factor; this one can. Empty means the bootstrap step does nothing.
    KEYCLOAK_OTP_ADMIN_USERNAME: str = ""
    KEYCLOAK_OTP_ADMIN_PASSWORD: str = ""
    KEYCLOAK_OTP_ADMIN_TOTP_SECRET: str = ""

    KEYCLOAK_ADMIN_CLIENT_ID: str = "opi-admin-service"
    KEYCLOAK_ADMIN_CLIENT_SECRET: str = ""

    # Shared OTP for project realm-admin accounts. Off by default so enabling the
    # feature is a deliberate, controlled rollout: when False, no OTP is
    # generated, stored, shown, or enforced anywhere. When True, newly created
    # realms get a shared OTP credential at creation and existing realms are
    # retrofitted (seed shown in the portal) the next time they are processed.
    KEYCLOAK_ENFORCE_ADMIN_OTP: bool = False

    # Require OTP for every password login in the master realm. Disable to troubleshoot
    # master console access issues.
    KEYCLOAK_ENFORCE_MASTER_OTP: bool = True

    # Retention of the login and admin events of the MASTER realm, in days. In Keycloak 0
    # means: never clean up. Configurable because BIO2 8.15.04 asks for a risk-based term
    # that cannot be decided from the code (open decision 7 in
    # features/futures/gebeurtenissen-plan-van-aanpak.md, recommendation: over 90 days).
    KEYCLOAK_MASTER_EVENTS_EXPIRATION_DAYS: int = 90

    # Default shared realm configuration
    KEYCLOAK_DEFAULT_REALM: str = "rig-platform"
    KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME: str = "ZAD"

    # Bootstrap configuration type: "default", "local", "sandbox" or "staging"
    # - "default": Production setup with direct SSO-Rijk integration
    # - "local": Local Kind cluster using production Keycloak as upstream IDP
    # - "sandbox": Sandbox cluster with upstream IDP (production Keycloak)
    # - "staging": upstream IDP like the sandbox, but no local admin user in OPI's realm
    # In all cases, OPI creates its own realm during startup using the project file config.
    KEYCLOAK_BOOTSTRAP_CONFIG: str = "default"

    # Master OIDC provider configuration (to be added to shared realm)
    KEYCLOAK_MASTER_OIDC_CLIENT_ID: str = "dummy-client-id"
    KEYCLOAK_MASTER_OIDC_CLIENT_SECRET: str = "dummy-client-secret-123"  # noqa: S105  # a dev default, overridden by the environment in a cluster
    KEYCLOAK_MASTER_OIDC_DISCOVERY_URL: str = "https://keycloak.kind/realms/master/.well-known/openid-configuration"

    # Database configuration
    DATABASE_HOST: str = "postgresql.kind"
    DATABASE_NAME: str = "operations_manager"
    DATABASE_ADMIN_NAME: str = "postgres"
    DATABASE_ADMIN_PASSWORD: str = "changeMe123!"  # noqa: S105  # a dev default, overridden by the environment in a cluster
    # The role OPI opens its OWN database with: owner of operations_manager, and without
    # superuser, CREATEDB or CREATEROLE. The admin credentials above stay for managing tenant
    # and platform databases. No default on the password, for the same reason as with
    # ARGOCD_*: a forgotten setting has to be an error.
    DATABASE_APP_USER: str = "operations_manager"
    DATABASE_APP_PASSWORD: str | None = None
    # libpq sslmode for OPI's connections to databases of the platform. require encrypts
    # without checking the server certificate (CNPG's internal CA). Local dev: set it to
    # disable. See features/postgresql-tls.md.
    DATABASE_SSLMODE: str = "require"

    # PostgreSQL grants a new database CONNECT and TEMPORARY to PUBLIC, so on the shared
    # server every role can connect to every database. This closes that off.
    #
    # ``dry-run`` is the default: the REVOKE touches every database on the server in one go,
    # and a role that quietly leans on PUBLIC falls over. In this mode every run logs which
    # REVOKE and GRANT it would do and who is connected at that moment, so a nightly
    # reconciliation makes visible what ``on`` is going to close off.
    DATABASE_RESTRICT_PUBLIC_CONNECT: Literal["off", "dry-run", "on"] = "dry-run"

    # Deletion grace period (days before marked resources are purged by reconciliation)
    DELETION_GRACE_PERIOD_DAYS: int = 7

    # Nightly reconciliation: unmark what reappeared and purge what is marked and expired.
    # Runs after the resource tuner's window. Dry-run is the default and logs what it would
    # purge without touching anything, so the marks in play are visible before unattended
    # deletion is switched on with RECONCILIATION_DRY_RUN=false in the cluster overlay.
    RECONCILIATION_SCHEDULER_ENABLED: bool = True
    RECONCILIATION_HOUR: int = 3  # local time (Europe/Amsterdam)
    RECONCILIATION_DRY_RUN: bool = True

    # The retention loop of the audit trail. No switch to turn it off: without the loop
    # notification_events keeps personal data without a term.
    AUDIT_RETENTION_HOUR: int = 4  # local time (Europe/Amsterdam)

    # Async task worker settings
    TASK_WORKER_ENABLED: bool = True
    TASK_WORKER_POLL_INTERVAL: float = 2.0
    TASK_WORKER_HEARTBEAT_INTERVAL: float = 30.0
    TASK_WORKER_STALE_THRESHOLD: int = 120
    TASK_WORKER_MAX_ATTEMPTS: int = 3
    TASK_WORKER_CONCURRENCY: int = 12
    TASK_WORKER_CLEANUP_RETENTION_HOURS: int = 1  # The working data (payload, logs, ...)
    TASK_WORKER_HEADER_RETENTION_DAYS: int = 90  # The row itself (who, what, where, ...)
    TASK_WORKER_MAX_DURATION: int = 1800  # 30 minutes

    # MinIO configuration
    MINIO_HOST: str = "minio.kind:9000"
    MINIO_ADMIN_ACCESS_KEY: str = "admin"
    MINIO_ADMIN_SECRET_KEY: str = "changeMe123!"  # noqa: S105  # a dev default, overridden by the environment in a cluster
    MINIO_USE_TLS: bool = False
    MINIO_REGION: str = "us-east-1"  # AWS region for S3 compatibility

    # MinIO client configuration
    MC_CONFIG_DIR: str = "/tmp/mc-config"  # Directory for mc CLI configuration files

    # SeaweedFS (seaweedfs-storage, RC-331). Both addresses are needed: without a master
    # address `weed shell` hangs in WaitUntilConnected (measured, row G18). Empty means "this
    # cluster does not offer the service"; the overlay of every cluster with it sets them.
    SEAWEEDFS_FILER_ADDRESS: str = ""
    SEAWEEDFS_MASTER_ADDRESS: str = ""
    # The admin key pair out of seaweedfs-credentials, which `mc` puts an alias on the S3
    # port with: for the bucket copy of a clone and for what `weed shell` cannot do, the
    # lifecycle rule on a bucket (RC-380). Managing identities does NOT run through here but
    # over `weed shell`.
    SEAWEEDFS_ADMIN_ACCESS_KEY: str = ""
    SEAWEEDFS_ADMIN_SECRET_KEY: str = ""

    # Redis configuration
    REDIS_HOST: str = "rig-redis.rig-system.svc.cluster.local"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = "changeMe123!"  # noqa: S105  # a dev default, overridden by the environment in a cluster

    # SMTP relay configuration (send-email service).
    # The relay is the only thing on the platform that talks to the upstream mail
    # server; everything else gets an account on it. Empty MAIL_RELAY_API_URL means
    # the relay is not deployed on this cluster and the service refuses to provision
    # rather than handing out credentials that lead nowhere.
    MAIL_RELAY_API_URL: str = ""  # Stalwart management API, e.g. http://rig-mail-relay.rig-prd-ron:8080
    MAIL_RELAY_ADMIN_USERNAME: str = "admin"
    MAIL_RELAY_ADMIN_PASSWORD: str = ""  # supports age:/base64+age:/plain: prefixes
    MAIL_RELAY_VERIFY_TLS: bool = True

    # The readiness watch on the relay (RC-261). OPI may already reach port 8080, so the
    # measurement costs nothing; the message goes to the ntfy topic of the log_watcher.
    MAIL_RELAY_CHECK_INTERVAL_SECONDS: int = 60
    MAIL_RELAY_DOWN_ALERT_SECONDS: int = 300  # only report after five minutes, a rollout may take a while

    # Temporary capacity watch on the tenant quota (opi/runtime/periodic/capacity_watcher.py); reports
    # through the ntfy topic of the log_watcher. The tenant is explicit: the cluster does not
    # know which one it serves.
    CAPACITY_WATCH_ENABLED: bool = False
    CAPACITY_WATCH_TENANT: str = ""  # Capsule tenant, e.g. rig-prd

    # ZAD's own account on the relay. It is an ORDINARY account on the relay, made by the
    # same connector call a project account is made by; only the caller differs (the boot
    # instead of a project run). So there is no password setting here: the password does
    # not exist until OPI generates it, which is after the relay is up. Where it is then
    # kept is ``MAIL_PLATFORM_SECRET_NAME`` below.
    # A stricter limit than a project account gets, because it carries password-reset
    # tokens: a bug in the project side must not be able to eat this account's budget.
    # It stands in the SAME flat namespace as the project accounts, so it must stay out of
    # their prefix (``MAIL_PROJECT_ACCOUNT_PREFIX``): a name inside it is reachable from
    # the project path again, and MailManager refuses that path rather than allow it.
    MAIL_PLATFORM_ACCOUNT: str = "zad-platform"
    MAIL_PLATFORM_MESSAGES_PER_DAY: int = 2000
    # The Secret in OPI's OWN namespace that holds the platform account's credentials.
    # A Secret and not an environment variable: OPI generates this password itself, and a
    # pod reads its environment once at start, so a value made after that start would only
    # arrive on a restart -- a boot order nobody can follow afterwards.
    MAIL_PLATFORM_SECRET_NAME: str = "zad-platform-mail-account"  # noqa: S105  # the name of the secret, not a secret

    # KEYCLOAK's own account on the relay, a SECOND platform account next to zad-platform:
    # ZAD writes to a project's people, Keycloak writes login mail to whoever is logging in,
    # and a bounce on the one has to be distinguishable from a bounce on the other.
    #
    # ONE account for ALL realms. Keycloak's mail does not go out over a realm's smtpServer:
    # an own EmailSenderProvider reads the relay out of the POD's environment
    # (images/keycloak-zad/extensions/relay-email-sender/), so no realm carries a destination
    # a tenant could redirect.
    #
    # The password is NOT here and is NOT generated by OPI. It comes out of the bootstrap
    # (infrastructure/.../secrets/templates/keycloak-mail-secret.yaml) because Keycloak has
    # to be able to have it without OPI ever having run. OPI only reconciles it onto the
    # relay; the Secret below is where it reads it.
    MAIL_KEYCLOAK_ACCOUNT: str = "zad-keycloak"
    MAIL_KEYCLOAK_MESSAGES_PER_DAY: int = 2000
    #: The bootstrap Secret in OPI's own namespace holding Keycloak's mail password.
    MAIL_KEYCLOAK_SECRET_NAME: str = "keycloak-mail-credentials"  # noqa: S105  # the name of the secret, not a secret
    #: The KEY inside that Secret. Load-bearing on two sides: it is the ``key`` in the
    #: secretKeyRef of the Keycloak deployment (which reads it as ZAD_MAIL_RELAY_PASSWORD)
    #: and the key OPI reads here. That secretKeyRef is ``optional: true``, so a rename on
    #: one side does not stop anything -- it just makes every login mail fail authentication
    #: at the relay, far away from the rename.
    MAIL_KEYCLOAK_SECRET_KEY: str = "smtp-password"  # noqa: S105  # the name of the key in the secret, not a secret
    #: The display name in front of the sender address of login mail.
    #:
    #: Login mail leaves under the cluster's BARE address (``get_mail_from_address``), the
    #: same one the portal uses, so the display name is the only thing that differs between
    #: ``zad-platform`` and ``zad-keycloak``.
    #:
    #: This is ONE account for ALL realms, so the name can never carry a project: wanting
    #: that ends the one-account arrangement.
    MAIL_KEYCLOAK_FROM_NAME: str = "Rijksapps"

    # Default budget and the line between choosing and requesting: up to this number a
    # project decides itself, above it requires platform approval (RC-400).
    MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY: int = 1000

    # Metrics backend configuration
    # "prometheus" = direct Prometheus access (local/dev)
    # "grafana" = query via Grafana API (ODCN production)
    METRICS_BACKEND: str = "prometheus"

    # Prometheus configuration (used when METRICS_BACKEND="prometheus")
    PROMETHEUS_URL: str = "http://prometheus.rig-system:9090"
    # External Prometheus URL for browser access (iframe in metrics explorer)
    # Falls back to PROMETHEUS_URL if not set
    PROMETHEUS_EXTERNAL_URL: str | None = None
    # Bearer token that Prometheus sends when scraping /metrics endpoints.
    # Applications can validate this to restrict access to metrics.
    # Sourced from the prometheus-metrics-auth secret.
    PROMETHEUS_METRICS_AUTH_TOKEN: str | None = None

    # Grafana configuration (used when METRICS_BACKEND="grafana")
    GRAFANA_URL: str = "http://grafana-service.rig-system.svc.cluster.local:3000"
    GRAFANA_TOKEN: str | None = None
    GRAFANA_DATASOURCE_UID: str | None = None  # Auto-discovered if not set
    GRAFANA_BILLING_DATASOURCE_UID: str | None = None  # UID of the billing Mimir datasource in Grafana

    # Log watcher: periodic Loki triage of this OPI's production logs, pushing an
    # ntfy notification for anything unexpected (reuses GRAFANA_URL / GRAFANA_TOKEN).
    LOGWATCHER_ENABLED: bool = False  # the on/off "start" flag; opt-in (needs a token + ntfy topic)
    LOGWATCHER_INTERVAL_SECONDS: int = 1800  # one cycle every 30 minutes
    LOGWATCHER_NTFY_TOPIC: str | None = None  # secret, unguessable ntfy topic (treat like a password)
    LOGWATCHER_NTFY_SERVER: str = "https://ntfy.sh"
    LOGWATCHER_NAMESPACE: str = "rig-prd-operations"  # namespace whose OPI logs to scan
    LOGWATCHER_CONTAINER: str = "operations-manager"  # container name in Loki labels
    LOGWATCHER_WINDOW: str = "now-35m"  # Loki look-back per run (30m cadence + 5m overlap)
    LOGWATCHER_DEDUP_HOURS: float = 6.0  # do not re-alert the same signature within this window

    # The rest of the resource-tuning parameters live in the service package
    # (opi/services/catalog/resource_tuning/config.py) and are not environment-driven.
    # Mirrors the upstream VPA recommender's podMinMemoryMb floor
    # (--pod-recommendation-min-allowed-memory-mb). A target at this value carries
    # no real signal (usage is below the floor), so fall back to Prometheus for the
    # memory request. Keep in sync with the recommender flag on the cluster.
    VPA_MEMORY_FLOOR_MI: int = 250
    # max_memory_limit_mi is a per-cluster setting in cluster_config.

    # Deployment sanitization configuration
    SANITIZE_RESTART_THRESHOLD: int = 10  # Restarts above this = broken

    # OOM watcher (fire-and-forget post-deploy check)
    OOM_WATCHER_ENABLED: bool = True
    OOM_WATCHER_DELAY_SECONDS: int = 120  # Wait before checking for OOM kills
    OOM_WATCHER_MAX_ATTEMPTS: int = 3  # Max tune cycles per deploy

    # Cluster-wide pod watch (OOM kills outside the deploy window). Whether it runs at
    # all is the cluster's call (``watches_pods_for_oom``); this only paces the sweep.
    OOM_METRIC_SWEEP_INTERVAL_SECONDS: int = 3600  # Safety-net query interval

    # How often the cluster is counted for pods that cannot pull their image. One kubectl
    # LIST per pass, and the alert on it only fires after 15 minutes, so a shorter
    # interval buys nothing.
    IMAGE_PULL_OBSERVE_INTERVAL_SECONDS: int = 60

    # Federation settings
    FEDERATION_ROLE: str = "standalone"  # standalone | master | slave
    FEDERATION_PEERS: str = ""  # JSON: [{"cluster":"local","url":"...","api_key":"..."}]
    FEDERATION_REQUEST_TIMEOUT: int = 30

    # Backup configuration
    # The image of the backup and restore pods, on digest so what runs is fixed. To raise it:
    # publish with `task publish-backup-image` and put the new digest here. The sandbox sets
    # it to :latest through its configmap, because there it is loaded into kind.
    BACKUP_IMAGE: str = (
        "ghcr.io/minbzk/base-images/rig-backup@sha256:3de60a9f38f9166a36e100561665eee86cc6a1723ccba5089803392b257a7e0c"
    )
    BACKUP_S3_ENDPOINT: str = "minio.rig-backup-destination.svc:9000"
    BACKUP_S3_BUCKET: str = "rig-backups"  # Fallback bucket when project context unavailable
    BACKUP_S3_BUCKET_MODE: str = "per-project"  # "single" = use BACKUP_S3_BUCKET, "per-project" = auto-generate
    BACKUP_S3_ORG_PREFIX: str = "rig"  # Organization prefix for per-project bucket names
    BACKUP_S3_ACCESS_KEY: str = "backup-admin"
    BACKUP_S3_SECRET_KEY: str = "backup-secret-key-local"  # noqa: S105  # a dev default, overridden by the environment in a cluster
    BACKUP_S3_USE_TLS: bool = False  # Use HTTP (False) or HTTPS (True) for S3 endpoint
    BACKUP_SNAPSHOT_CLASS: str = "ocs-storagecluster-rbdplugin-snapclass"
    BACKUP_TIMEOUT_SECONDS: int = 3600
    # Retention: Kopia keeps the union of all "keep-*" rules. With these
    # defaults a daily-scheduled deployment retains 30 daily snapshots, then
    # one per week for 4 weeks, then one per month for 12 months. Manual
    # backups bypass retention entirely (separate Kopia source identity) and
    # are removed only when an operator deletes them explicitly.
    BACKUP_RETENTION_KEEP_LATEST: int = 30
    BACKUP_RETENTION_KEEP_DAILY: int = 30
    BACKUP_RETENTION_KEEP_WEEKLY: int = 4
    BACKUP_RETENTION_KEEP_MONTHLY: int = 12

    # Backup scheduler settings. Ticks are anchored to wall-clock boundaries
    # of this interval (e.g. 600 -> HH:00, HH:10, HH:20, ...), so the firing
    # phase is independent of pod start time.
    BACKUP_SCHEDULER_ENABLED: bool = True
    BACKUP_SCHEDULER_INTERVAL: int = 600  # seconds between schedule checks
    BACKUP_MAX_CONCURRENT: int = 2  # max backup/restore tasks running simultaneously
    # Catch-up window after the configured BYHOUR:BYMINUTE. If the scheduled
    # moment has passed and no backup has completed today, fire any time within
    # this window (default 4 hours). Outside it, skip and wait for tomorrow, which
    # prevents an OPI deploy at 18:00 from firing a "missed" 02:00 backup at
    # 18:00. Bumps up if your downtime windows are longer than 4h.
    BACKUP_SCHEDULE_CATCH_UP_SECONDS: int = 14400
    # How long a task waits for the global backup lock before giving up.
    # Cron-anchored ticks fire multiple due backups at the same instant, and
    # without a wait the second one fails immediately. 30 min is enough for
    # a typical PVC+DB+bucket run to finish.
    BACKUP_LOCK_WAIT_SECONDS: int = 1800
    # How often a deployment whose scheduled backup is not happening may produce an
    # ERROR line. The log watcher turns that line into a notification, and the scheduler
    # sees the same overdue backup on every tick until it is fixed.
    BACKUP_OVERDUE_LOG_INTERVAL_HOURS: int = 24
    # Daily retention sweep for orphaned backups. Kopia retention only runs
    # at the end of a backup, so snapshots of deployments that no longer get
    # backed up (deleted deployments, removed schedules, broken legacy source
    # identities) would otherwise live forever. The sweep deletes orphaned
    # non-manual snapshots once they are older than the grace period. Manual
    # backups are never touched. Dry-run mode only logs what would be
    # deleted; disable it after reviewing a sweep manifest in the logs.
    BACKUP_SWEEP_ENABLED: bool = True
    BACKUP_SWEEP_DRY_RUN: bool = True
    BACKUP_ORPHAN_RETENTION_DAYS: int = 30

    # Sleep-mode: scale idle preview deployments to zero after a deadline and wake
    # them on request. These are operational toggles (env-overridable); the actual
    # sleep-mode config and its cluster-wide default are owned by the service package
    # (opi/services/catalog/sleep_mode).
    SLEEP_MODE_SCHEDULER_ENABLED: bool = True
    SLEEP_MODE_SWEEP_MINUTES: int = 30  # how often the sweeper checks deadlines
    SLEEP_MODE_PACE_SECONDS: int = 15  # delay between changed projects, to spread commits
    SLEEP_MODE_WAKING_TIMEOUT_MINUTES: int = 10  # revert a stuck `waking` back to `awake`
    SLEEP_MODE_WAKER_IMAGE: str = "ghcr.io/minbzk/base-images/zad-waker:latest"

    # Ephemeral database console (on-request, auto-expiring web DB client).
    # OPI applies/removes these directly (outside git/ArgoCD); a reaper enforces
    # the TTL and a per-pod activeDeadlineSeconds is the hard backstop.
    DB_CONSOLE_ENABLED: bool = True
    DB_CONSOLE_TTL_SECONDS: int = 3600  # Session lifetime (default 1 hour)
    # The pod's own activeDeadlineSeconds is what ends an expired session, so the
    # sweep only clears leftovers (Secret, ConfigMap, Service, Ingress, OIDC client).
    # Doing that within a minute instead of within a quarter buys nothing, and the
    # orphan-client GC calls Keycloak on every pass.
    DB_CONSOLE_REAP_INTERVAL_SECONDS: int = 900
    # Tool images. Defaults are docker.io refs for local/dev; production overlays
    # MUST override these with a mirror reachable on the cluster (e.g. rcr.rijksapps.nl),
    # since docker.io/ghcr are blocked on ODCN.
    DB_CONSOLE_PGWEB_IMAGE: str = "sosedoff/pgweb:0.16.2"
    DB_CONSOLE_DBGATE_IMAGE: str = "dbgate/dbgate:6.6.1"

    # TransIP DNS API, used to keep CAA records on the zones we administer.
    # Their presence is the on/off switch: without credentials the CAA reconciler
    # skips, so sandbox and local do nothing by themselves.
    TRANSIP_ACCOUNT_NAME: str | None = None
    TRANSIP_PRIVATE_KEY: str | None = None  # PEM, the whole key

    # Ad-hoc job runs (run an image + command once; a "run" like the console).
    # Independently toggleable and TTL'd; the shared run reaper sweeps both kinds.
    JOB_ENABLED: bool = True
    JOB_TTL_SECONDS: int = 3600  # Max job lifetime; pod kept until then so logs stay readable

    @property
    def database_app_password(self) -> str:
        """The password of the app role, or an error when the setting is empty.

        Without this check a forgotten ``DATABASE_APP_PASSWORD`` only falls over as an
        authentication error from asyncpg, and that one does not point at the setting.
        """
        if not self.DATABASE_APP_PASSWORD:
            raise ValueError(
                "DATABASE_APP_PASSWORD is niet gezet. OPI opent zijn eigen database met de rol "
                f"'{self.DATABASE_APP_USER}'; het wachtwoord komt uit het secret operations-manager-db-credentials."
            )
        return self.DATABASE_APP_PASSWORD

    @property
    def is_development_environment(self) -> bool:
        """Local or sandbox: the only places where proof and test routes may exist."""
        return self.ENVIRONMENT in ("local", "sandboxed-local")

    @model_validator(mode="after")
    def _enforce_secure_secret_key(self) -> Settings:
        """Fail closed when an explicitly-set SECRET_KEY is too short."""
        validate_secret_key(self.SECRET_KEY)
        return self

    @model_validator(mode="after")
    def _only_the_sandbox_may_hold_a_proof_token(self) -> Settings:
        """Refuse a proof token on every cluster except the sandbox."""
        if self.SECURITY_PROOF_TOKEN and self.CLUSTER_MANAGER != PROOF_TARGET_CLUSTER:
            raise RuntimeError(
                f"SECURITY_PROOF_TOKEN is gezet op cluster {self.CLUSTER_MANAGER!r};"
                f" alleen {PROOF_TARGET_CLUSTER} mag een doel van de aanvalsproef zijn"
            )
        return self


def format_age_key_file(private_key: str, public_key: str) -> str:
    """An AGE key file as text: the form the Secret ``sops-age-key`` carries.

    Next to :func:`parse_sops_age_key_content`, which reads it back. The Kopia password of
    a namespace is derived from this whole text, so predicting what lands there after a key
    replacement means producing exactly these bytes.
    """
    return f"# created: project-specific key\n# public key: {public_key}\n{private_key}\n"


def parse_sops_age_key_content(content: str) -> tuple[str | None, str | None]:
    """Returns the pair ``(public_key, private_key)`` out of the multiline SOPS age key content."""
    if not content:
        return None, None

    try:
        public_key = None
        private_key = None

        for line in content.splitlines():
            line = line.strip()
            if line.startswith("# public key:"):
                # Extract public key from comment line
                public_key = line.split(":", 1)[1].strip()
            elif line.startswith("AGE-SECRET-KEY-"):
                # This is the private key
                private_key = line.strip()

        logger.debug("Parsed SOPS age keys from environment content")
        if public_key:
            logger.debug("Public key parsed")
        if private_key:
            logger.debug("Private key parsed")

        return public_key, private_key

    except Exception as e:
        logger.error(f"Error parsing SOPS age key content: {e}")
        return None, None


def load_sops_age_key_content(
    key_file: str | None, key_content: str | None, *, label: str = "Platformsleutel"
) -> str | None:
    """The content of an AGE key, out of the mounted file or out of the env fallback.

    A file that is set but unreadable or empty is a hard startup error: quietly falling back
    on the environment would hide a wrongly mounted key until the first decryption.
    """
    if key_file:
        try:
            content = pathlib.Path(key_file).read_text().strip()
        except OSError as e:
            raise RuntimeError(f"{label} niet leesbaar: {key_file}") from e
        if not content:
            raise RuntimeError(f"{label} is leeg: {key_file}")
        return content

    if key_content:
        logger.warning(
            "SOPS_AGE_KEY_CONTENT is in gebruik; die terugval verdwijnt na een release. "
            "Mount de sleutel en zet SOPS_AGE_KEY_FILE."
        )
        return key_content

    return None


def _load_sops_key_from_local_file() -> str | None:
    """The local file fallback for development environments, where there is no Kubernetes secret
    to mount. None when the file is not there or not readable.
    """
    logger.warning("ATTEMPTING TO READ SOPS KEY FROM LOCAL FILE - THIS IS FOR DEVELOPMENT ONLY!")
    logger.warning("In production, SOPS_AGE_KEY_CONTENT should be provided via Kubernetes secret")

    try:
        # Get the path to security/key.txt (2 levels up from working directory)
        # Working directory is operations-manager/python, so go up 2 levels to RIG-Cluster
        working_dir = pathlib.Path.cwd()  # operations-manager/python
        operations_dir = working_dir.parent  # operations-manager/
        rig_cluster_dir = operations_dir.parent  # RIG-Cluster/
        key_file_path = rig_cluster_dir / "security" / "key.txt"

        logger.warning(f"Attempting to read SOPS key from: {key_file_path}")

        if key_file_path.exists():
            with open(key_file_path) as f:
                local_key_content = f.read().strip()

            if local_key_content:
                logger.warning("Successfully read SOPS key from local file")
                logger.warning("LOCAL SOPS KEY LOADED - REMEMBER TO CONFIGURE KUBERNETES SECRET FOR PRODUCTION!")
                return local_key_content
            else:
                logger.error(f"Local SOPS key file is empty: {key_file_path}")
                return None
        else:
            logger.error(f"Local SOPS key file not found: {key_file_path}")
            logger.error("Expected file structure: RIG-Cluster/security/key.txt")
            return None

    except Exception as e:
        logger.error(f"Failed to read local SOPS key file: {e}")
        logger.error("SOPS operations may fail without proper key configuration")
        return None


def _load_project_kek(settings: Settings) -> None:
    """Set the project KEK out of the mounted file, when it is there.

    Without a KEK nothing changes: wrapping and unwrapping project keys then falls back on
    the platform key, so the sandbox and local development need nothing.
    """
    key_file = settings.PROJECT_KEK_FILE
    if key_file and not pathlib.Path(key_file).exists():
        # The secret is optional in the deployment, so without a secret the kubelet mounts an
        # empty directory. "No KEK" is then the intended outcome and not a startup error; a
        # file that IS there but is empty or unreadable stays one.
        logger.warning(f"Project-KEK niet gemount op {key_file}; projectsleutels blijven op de platformsleutel")
        key_file = None
    content = load_sops_age_key_content(key_file, None, label="Project-KEK")
    if content:
        public_key, private_key = parse_sops_age_key_content(content)
        if public_key and not settings.PROJECT_KEK_PUBLIC_KEY:
            settings.PROJECT_KEK_PUBLIC_KEY = public_key
        if private_key and not settings.PROJECT_KEK_PRIVATE_KEY:
            settings.PROJECT_KEK_PRIVATE_KEY = private_key
        if not (settings.PROJECT_KEK_PUBLIC_KEY and settings.PROJECT_KEK_PRIVATE_KEY):
            raise RuntimeError(f"Project-KEK bevat geen AGE-sleutelpaar: {settings.PROJECT_KEK_FILE}")
    logger.info(f"Settings PROJECT_KEK_FILE: {settings.PROJECT_KEK_FILE or 'NOT SET'}")
    logger.info(f"Settings PROJECT_KEK: {'SET' if settings.PROJECT_KEK_PRIVATE_KEY else 'NOT SET'}")


def _get_settings() -> Settings:
    settings = Settings()

    setup_logging(
        log_to_file=settings.LOG_TO_FILE,
        log_file_path=settings.LOG_FILE_PATH,
        log_errors_to_file=settings.LOG_ERRORS_TO_FILE,
        log_errors_file_path=settings.LOG_ERRORS_FILE_PATH,
        level=settings.LOG_LEVEL,
    )

    # Detailed logging for SOPS key configuration
    logger.info("=== SOPS Age Key Configuration Debug ===")

    settings.SOPS_AGE_KEY_CONTENT = load_sops_age_key_content(settings.SOPS_AGE_KEY_FILE, settings.SOPS_AGE_KEY_CONTENT)
    # After it has been read the key should not stay in the environment: every child process
    # would inherit it after all. child_env() keeps it out too; this is the source itself.
    os.environ.pop("SOPS_AGE_KEY_CONTENT", None)

    logger.info(f"Settings SOPS_AGE_KEY_FILE: {settings.SOPS_AGE_KEY_FILE or 'NOT SET'}")
    logger.info(f"Settings SOPS_AGE_KEY_CONTENT: {'SET' if settings.SOPS_AGE_KEY_CONTENT else 'NOT SET'}")
    logger.info(f"Settings SOPS_AGE_PUBLIC_KEY: {'SET' if settings.SOPS_AGE_PUBLIC_KEY else 'NOT SET'}")
    logger.info(f"Settings SOPS_AGE_PRIVATE_KEY: {'SET' if settings.SOPS_AGE_PRIVATE_KEY else 'NOT SET'}")

    # Parse SOPS age key content if provided
    if settings.SOPS_AGE_KEY_CONTENT and not (settings.SOPS_AGE_PUBLIC_KEY and settings.SOPS_AGE_PRIVATE_KEY):
        logger.info("Parsing SOPS age key content...")
        public_key, private_key = parse_sops_age_key_content(settings.SOPS_AGE_KEY_CONTENT)

        if public_key and not settings.SOPS_AGE_PUBLIC_KEY:
            settings.SOPS_AGE_PUBLIC_KEY = public_key
            logger.info(f"Parsed public key: {public_key}")
        if private_key and not settings.SOPS_AGE_PRIVATE_KEY:
            settings.SOPS_AGE_PRIVATE_KEY = private_key
            logger.info("Parsed private key")

        logger.info("Successfully parsed SOPS age keys from content")
    elif settings.SOPS_AGE_KEY_CONTENT:
        logger.info("SOPS age key content provided but keys already set individually")
    else:
        logger.warning("No SOPS age key content provided in environment")

        # Try to load from local file for development
        local_key_content = _load_sops_key_from_local_file()
        if local_key_content:
            # Set the SOPS_AGE_KEY_CONTENT so it goes through the regular processing flow
            settings.SOPS_AGE_KEY_CONTENT = local_key_content

            # Now parse it using the regular flow
            logger.info("Parsing SOPS age key content from local file...")
            public_key, private_key = parse_sops_age_key_content(settings.SOPS_AGE_KEY_CONTENT)

            if public_key and not settings.SOPS_AGE_PUBLIC_KEY:
                settings.SOPS_AGE_PUBLIC_KEY = public_key
                logger.info(f"Parsed public key: {public_key}")
            if private_key and not settings.SOPS_AGE_PRIVATE_KEY:
                settings.SOPS_AGE_PRIVATE_KEY = private_key
                logger.info("Parsed private key")

            logger.info("Successfully parsed SOPS age keys from local file content")

    _load_project_kek(settings)

    logger.debug(f"Settings loaded with API_TOKEN: {'SET' if settings.API_TOKEN else 'NOT SET'}")
    if settings.SOPS_AGE_PUBLIC_KEY:
        logger.debug("SOPS public key available")
    if settings.SOPS_AGE_PRIVATE_KEY:
        logger.debug("SOPS private key available")

    return settings


settings = _get_settings()
