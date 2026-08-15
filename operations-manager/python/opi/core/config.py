from __future__ import annotations

import logging
import os
import pathlib
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

# Initialize logging early to ensure it's available during config loading
from opi.core.early_logging import initialize_logging  # noqa: F401 (side-effect import)
from opi.core.secret_key import generate_secret_key, validate_secret_key
from opi.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)

PROJECT_NAME: str = "OPI"
VERSION: str = os.environ.get("ZAD_VERSION", "0.1.0")
BUILD_DATE: str = os.environ.get("ZAD_BUILD_DATE", "")
PROJECT_DESCRIPTION: str = "OPI - Operational Platform Interface"


def _check_env_file_for_environment_var(file_path: str) -> None:
    """
    Check if an .env file contains ENVIRONMENT variable and warn if found.

    Args:
        file_path: Path to the .env file to check
    """
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
    """
    Get list of environment files to load in order of precedence.

    Configuration hierarchy (container env vars take highest precedence):
    1. Container environment variables (from Kubernetes secrets) - HIGHEST PRECEDENCE
    2. ConfigMap mounted .env file (container/Kubernetes overrides)
    3. .env.{ENVIRONMENT} (environment-specific files, only for environments in ENVIRONMENT list)
    4. .env (base configuration file - always loaded) - LOWEST PRECEDENCE

    ENVIRONMENT is read from system environment variable first to avoid circular dependency.
    ENVIRONMENT can be a single value or comma-separated list (e.g., "production,kubernetes").
    If ENVIRONMENT is not set in system env, defaults to 'local'.

    Examples:
    - ENVIRONMENT="local" -> loads .env, then .env.local
    - ENVIRONMENT="production" -> loads .env, then .env.production
    - ENVIRONMENT="production,kubernetes" -> loads .env, then .env.production, then .env.kubernetes

    Returns:
        List of environment file paths that exist
    """
    global _env_files_cache

    # Return cached result to avoid duplicate logging and processing
    if _env_files_cache is not None:
        return _env_files_cache
    env_files = []

    # Get ENVIRONMENT from system environment variable first (not from .env files to avoid circular dependency)
    environment_var = os.environ.get("ENVIRONMENT", "local")
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
        # TODO: do we want to support this? and if so, we should document it
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
    # reported, not fatal.
    #
    # Forbidding meant an OPI image refused to start on a config file that mentions a
    # setting newer than itself, which is exactly what a rollback or an upgrade test does.
    # It cost the upgrade-safety run twice: the baseline image crash-looped on SLEEP_MODE_*
    # and the operator had to strip those lines from the live ConfigMap by hand, then put
    # them back before swapping to the new image. The second time one line was missed
    # (KEYCLOAK_ENFORCE_ADMIN_OTP), so the new side ran without OTP and the test could not
    # show anything about it -- silently, because nothing complains about a setting that is
    # simply absent.
    #
    # The reason for forbidding was catching typos in configuration, and that is worth
    # keeping, so unknown keys are logged as a warning naming each one (see
    # ``_warn_about_unknown_settings``). A typo still surfaces; a rollback no longer bricks.
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
    ENVIRONMENT: str = "local"
    DEBUG: bool = False
    CLUSTER_MANAGER: str = "local"

    # Idle session timeout. The session cookie is re-signed on every response,
    # so this acts as a sliding window: a session expires only after this many
    # seconds of inactivity. Shared by the HTTP SessionMiddleware and the
    # WebSocket handshake so both enforce the same lifetime.
    SESSION_MAX_AGE_SECONDS: int = 28800  # 8 hours - one workday

    # Development mode: "reload" (hot-reload) | "debug" (debugger) | "production"
    DEBUG_MODE: str = "reload"

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
    CLI_TOKEN_AUDIENCE: str = "zad-api"

    # Invite system settings
    INVITE_CLIENT_ID: str = "operations-manager-invites"

    # Access control settings
    ALLOWED_EMAILS: str | None = None  # Comma-separated list of allowed email addresses
    ADMIN_EMAILS: str | None = None  # Comma-separated list of admin emails (can view all projects)

    # Git projects server settings - for monitoring and retrieving project files
    ENABLE_GIT_MONITOR: bool = False
    GIT_PROJECTS_SERVER_URL: str = "git://localhost:9090/"
    GIT_PROJECTS_SERVER_USERNAME: str | None = None  # Username for Git projects server authentication
    GIT_PROJECTS_SERVER_PASSWORD: str | None = None  # Password for Git projects server (can be SOPS encrypted)
    GIT_PROJECTS_SERVER_REPO_PATH: str = "/"
    GIT_PROJECTS_SERVER_FILE_PATH: str = "projects/simple-example.yaml"
    GIT_PROJECTS_SERVER_BRANCH: str = "main"
    GIT_PROJECTS_SERVER_POLL_INTERVAL: int = 120  # seconds
    # Fallback poll for edits made outside ZAD (by hand, or from another cluster).
    # Bounds how long an out-of-band revocation (member removed, invite key revoked
    # by pushing to zad-projects directly) can keep working on this instance.
    # Cheap when idle: reconcile() starts with an ls-remote check. 0 disables.
    PROJECT_STORE_RECONCILE_INTERVAL_SECONDS: int = 300

    # Project deployment repository - shared repo where all project manifests are pushed
    # This is a single shared repository; projects are separated by subdirectories internally
    PROJECT_REPO_URL: str = "https://github.com/RijksICTGilde/rig-cluster-application-test.git"
    PROJECT_REPO_USERNAME: str = "git"
    PROJECT_REPO_PASSWORD: str = "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NCd1lVWk1ZVFZ1Ukd4dmJrUjZNelZRCksxbFVVVE5qWm5wNllYRjBUVXBsWjJWc2VuSjRRa00xZW1wdkNpOUdNakpQVldSTWNrUnRSakUyWTNObVlXcFUKZEZOMGMzZHlVbG8wY0ZkTFQwWnhhWFZ4U21wVmNVMEtMUzB0SUdReFJtUldWSGRhTVdVd1dqaHRSVW92WnlzeQpkVTlNWmpSMFZWSjFTWFZIVDFZd2NIZFZVekJwY1RnSzNvYVR4b3YwRW1RcVkrRjlTWkgzVjBONHFXd25ESEllCjI4U05ud2ZxaWthQWE1dGNWcmIvOW4xM3BLN3NEQVQ2bXpZS3NKeFhxdDV0UnpJeWxUWHk5dkk0REticmRiSmkKLS0tLS1FTkQgQUdFIEVOQ1JZUFRFRCBGSUxFLS0tLS0="
    PROJECT_REPO_BRANCH: str = "main"

    # ArgoCD Applications Git repository - simplified to just URL and credentials
    GIT_ARGO_APPLICATIONS_URL: str = "ssh://git@localhost:2222/srv/git/argo-applications.git"
    GIT_ARGO_APPLICATIONS_PASSWORD: str | None = None
    GIT_ARGO_APPLICATIONS_BRANCH: str = "main"
    GIT_ARGO_APPLICATIONS_USERNAME: str | None = None

    ARGOCD_MANAGER: str = "rig-system"

    # ArgoCD Server Configuration
    ARGOCD_HOST: str = "argocd-server"
    ARGOCD_PORT: int = 80
    ARGOCD_USERNAME: str = "admin"
    ARGOCD_PASSWORD: str = "admin"
    ARGOCD_USE_TLS: bool = False
    ARGOCD_VERIFY_SSL: bool = False

    # Manifests path
    MANIFESTS_PATH: str = "manifests"

    # API security
    API_TOKEN: str = "d68d6aebd694d636e5eb4784a952b9c3"  # Example hardcoded token for development
    USE_UNSAFE_API_KEY: bool = False  # Use hardcoded "secret" API key for development (set to True in .env.local)
    MASTER_API_KEY: str | None = None  # Master API key for admin operations (backups, etc.)
    ADMIN_API_KEY: str | None = None  # Admin API key for cleanup, reconciliation, and maintenance operations

    # SOPS age key settings (from Kubernetes secret)
    SOPS_AGE_KEY_CONTENT: str | None = None  # Full SOPS age key content from secret
    SOPS_AGE_PUBLIC_KEY: str | None = None  # Public key for SOPS age encryption
    SOPS_AGE_PRIVATE_KEY: str | None = None  # Private key for SOPS age decryption

    # OpenTelemetry configuration
    OTEL_ENABLED: bool = False  # Safe default - zero overhead when off
    OTEL_SERVICE_NAME: str = "opi-operations-manager"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://jaeger.rig-system:4317"
    OTEL_TRACES_SAMPLER_ARG: str = "1.0"  # 1.0 = 100% sampling for dev
    OTEL_LOG_CORRELATION: bool = True

    # Prometheus metrics configuration
    ENABLE_TRACEMALLOC: bool = False  # Enable tracemalloc for Python memory allocation tracking (adds ~10-30% overhead)

    # Logging configuration
    LOG_TO_FILE: bool = False  # Enable file logging alongside stdout
    LOG_FILE_PATH: str = "log.txt"  # Path to log file when LOG_TO_FILE is enabled
    LOG_ERRORS_TO_FILE: bool = False  # Write WARNING+ to a separate persistent error log
    LOG_ERRORS_FILE_PATH: str = "/data/logs/errors.log"  # Path to error log (should be on PVC)

    # Temporary directory configuration
    TEMP_DIR: str = "/tmp"  # Default temp directory, can be overridden by TMPDIR env var

    # Container registry configuration (for image upload proxy)
    REGISTRY_URL: str = ""  # Target registry (e.g., rcr.rijksapps.nl)
    REGISTRY_ORG: str = ""  # Organization prefix (e.g., rig)
    REGISTRY_USERNAME: str = ""  # Robot account name
    REGISTRY_PASSWORD: str = ""  # Robot token (supports age:/base64+age:/plain: prefixes)
    REGISTRY_VERIFY_TLS: bool = True
    IMAGE_UPLOAD_MAX_SIZE_MB: int = 5120  # Safety cap (5 GB)

    # Keycloak configuration
    KEYCLOAK_URL: str = "https://keycloak.kind"
    KEYCLOAK_ADMIN_USERNAME: str = "admin"
    KEYCLOAK_ADMIN_PASSWORD: str = "changeMe123!"

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

    # Default shared realm configuration
    KEYCLOAK_DEFAULT_REALM: str = "rig-platform"
    KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME: str = "RIG Platform"

    # Bootstrap configuration type: "default", "local", or "sandbox"
    # - "default": Production setup with direct SSO-Rijk integration
    # - "local": Local Kind cluster using production Keycloak as upstream IDP
    # - "sandbox": Sandbox cluster with upstream IDP (production Keycloak)
    # In all cases, OPI creates its own realm during startup using the project file config.
    KEYCLOAK_BOOTSTRAP_CONFIG: str = "default"

    # Master OIDC provider configuration (to be added to shared realm)
    KEYCLOAK_MASTER_OIDC_CLIENT_ID: str = "dummy-client-id"
    KEYCLOAK_MASTER_OIDC_CLIENT_SECRET: str = "dummy-client-secret-123"
    KEYCLOAK_MASTER_OIDC_DISCOVERY_URL: str = "https://keycloak.kind/realms/master/.well-known/openid-configuration"

    # Database configuration
    DATABASE_HOST: str = "postgresql.kind"
    DATABASE_NAME: str = "operations_manager"
    DATABASE_ADMIN_NAME: str = "postgres"
    DATABASE_ADMIN_PASSWORD: str = "changeMe123!"

    # Deletion grace period (days before marked resources are purged by reconciliation)
    DELETION_GRACE_PERIOD_DAYS: int = 7

    # Async task worker settings
    TASK_WORKER_ENABLED: bool = True
    TASK_WORKER_POLL_INTERVAL: float = 2.0
    TASK_WORKER_HEARTBEAT_INTERVAL: float = 30.0
    TASK_WORKER_STALE_THRESHOLD: int = 120
    TASK_WORKER_MAX_ATTEMPTS: int = 3
    TASK_WORKER_CONCURRENCY: int = 12
    TASK_WORKER_CLEANUP_RETENTION_HOURS: int = 1
    TASK_WORKER_MAX_DURATION: int = 1800  # 30 minutes

    # MinIO configuration
    MINIO_HOST: str = "minio.kind:9000"
    MINIO_ADMIN_ACCESS_KEY: str = "admin"
    MINIO_ADMIN_SECRET_KEY: str = "changeMe123!"
    MINIO_USE_TLS: bool = False
    MINIO_REGION: str = "us-east-1"  # AWS region for S3 compatibility

    # MinIO client configuration
    MC_CONFIG_DIR: str = "/tmp/mc-config"  # Directory for mc CLI configuration files

    # Redis configuration
    REDIS_HOST: str = "rig-redis.rig-system.svc.cluster.local"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = "changeMe123!"

    # SMTP relay configuration (send-email service).
    # The relay is the only thing on the platform that talks to the upstream mail
    # server; everything else gets an account on it. Empty MAIL_RELAY_API_URL means
    # the relay is not deployed on this cluster and the service refuses to provision
    # rather than handing out credentials that lead nowhere.
    MAIL_RELAY_API_URL: str = ""  # Stalwart management API, e.g. http://rig-mail-relay.rig-operations-ron:8080
    MAIL_RELAY_ADMIN_USERNAME: str = "admin"
    MAIL_RELAY_ADMIN_PASSWORD: str = ""  # supports age:/base64+age:/plain: prefixes
    MAIL_RELAY_VERIFY_TLS: bool = True

    # ZAD's own account on the relay. It is not a project, so it has no project file to
    # hang on; its password comes from the SOPS secret in the relay's namespace and is
    # read here like any other platform secret (plans/mailrelay.md, aanvulling 4).
    # A stricter limit than a project account gets, because it carries password-reset
    # tokens: a bug in the project side must not be able to eat this account's budget.
    MAIL_PLATFORM_ACCOUNT: str = "zad-platform"
    MAIL_PLATFORM_PASSWORD: str = ""
    MAIL_PLATFORM_FROM_LOCAL_PART: str = "noreply"
    MAIL_PLATFORM_FROM_NAME: str = "ZAD"
    MAIL_PLATFORM_MESSAGES_PER_DAY: int = 2000

    # Default daily message budget for a project account when it sets none itself.
    MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY: int = 500

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

    # Resource tuning parameters moved to the resource-tuning service package
    # (opi/services/catalog/resource_tuning/config.py): a system service owns its own
    # config as a validated dict, and these have never been environment-driven.
    # Mirrors the upstream VPA recommender's podMinMemoryMb floor
    # (--pod-recommendation-min-allowed-memory-mb). A target at this value carries
    # no real signal (usage is below the floor), so fall back to Prometheus for the
    # memory request. Keep in sync with the recommender flag on the cluster.
    VPA_MEMORY_FLOOR_MI: int = 250
    # max_memory_limit_mi is now in cluster_config (per-cluster setting)

    # Deployment sanitization configuration
    SANITIZE_RESTART_THRESHOLD: int = 10  # Restarts above this = broken

    # OOM watcher (fire-and-forget post-deploy check)
    OOM_WATCHER_ENABLED: bool = True
    OOM_WATCHER_DELAY_SECONDS: int = 120  # Wait before checking for OOM kills
    OOM_WATCHER_MAX_ATTEMPTS: int = 3  # Max tune cycles per deploy

    # Federation settings
    FEDERATION_ROLE: str = "standalone"  # standalone | master | slave
    FEDERATION_PEERS: str = ""  # JSON: [{"cluster":"local","url":"...","api_key":"..."}]
    FEDERATION_REQUEST_TIMEOUT: int = 30

    # Backup configuration
    BACKUP_S3_ENDPOINT: str = "minio.rig-backup-destination.svc:9000"
    BACKUP_S3_BUCKET: str = "rig-backups"  # Fallback bucket when project context unavailable
    BACKUP_S3_BUCKET_MODE: str = "per-project"  # "single" = use BACKUP_S3_BUCKET, "per-project" = auto-generate
    BACKUP_S3_ORG_PREFIX: str = "rig"  # Organization prefix for per-project bucket names
    BACKUP_S3_ACCESS_KEY: str = "backup-admin"
    BACKUP_S3_SECRET_KEY: str = "backup-secret-key-local"
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
    # this window (default 4 hours). Outside it, skip and wait for tomorrow —
    # prevents an OPI deploy at 18:00 from firing a "missed" 02:00 backup at
    # 18:00. Bumps up if your downtime windows are longer than 4h.
    BACKUP_SCHEDULE_CATCH_UP_SECONDS: int = 14400
    # How long a task waits for the global backup lock before giving up.
    # Cron-anchored ticks fire multiple due backups at the same instant —
    # without a wait, the second one fails immediately. 30 min is enough for
    # a typical PVC+DB+bucket run to finish.
    BACKUP_LOCK_WAIT_SECONDS: int = 1800
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

    # Ad-hoc job runs (run an image + command once; a "run" like the console).
    # Independently toggleable and TTL'd; the shared run reaper sweeps both kinds.
    JOB_ENABLED: bool = True
    JOB_TTL_SECONDS: int = 3600  # Max job lifetime; pod kept until then so logs stay readable

    @model_validator(mode="after")
    def _enforce_secure_secret_key(self) -> Settings:
        """Fail closed when an explicitly-set SECRET_KEY is too short."""
        validate_secret_key(self.SECRET_KEY)
        return self


def parse_sops_age_key_content(content: str) -> tuple[str | None, str | None]:
    """
    Parse SOPS age key content to extract public and private keys.

    Args:
        content: The content of the SOPS age key (multiline string)

    Returns:
        Tuple of (public_key, private_key)
    """
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
            logger.debug(f"Public key: {public_key[:10]}...")
        if private_key:
            logger.debug(f"Private key: {private_key[:2]}...{private_key[-2:]}")

        return public_key, private_key

    except Exception as e:
        logger.error(f"Error parsing SOPS age key content: {e}")
        return None, None


def _load_sops_key_from_local_file() -> str | None:
    """
    Load SOPS key content from local file for development environments.

    FIXME: Remove this local file fallback when proper Kubernetes secret is configured.
    This is a temporary solution for local development environments.

    Returns:
        SOPS key content from local file, or None if not found/readable
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


def _get_settings() -> Settings:
    settings = Settings()

    setup_logging(
        log_to_file=settings.LOG_TO_FILE,
        log_file_path=settings.LOG_FILE_PATH,
        log_errors_to_file=settings.LOG_ERRORS_TO_FILE,
        log_errors_file_path=settings.LOG_ERRORS_FILE_PATH,
    )

    # Detailed logging for SOPS key configuration
    logger.info("=== SOPS Age Key Configuration Debug ===")

    logger.info(f"Environment SOPS_AGE_KEY_CONTENT: {'SET' if os.environ.get('SOPS_AGE_KEY_CONTENT') else 'NOT SET'}")
    if os.environ.get("SOPS_AGE_KEY_CONTENT"):
        content_length = len(os.environ.get("SOPS_AGE_KEY_CONTENT", ""))
        logger.info(f"SOPS_AGE_KEY_CONTENT length: {content_length} characters")

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
            logger.info(f"Parsed private key: {private_key[:2]}...{private_key[-2:]}")

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
                logger.info(f"Parsed private key: {private_key[:2]}...{private_key[-2:]}")

            logger.info("Successfully parsed SOPS age keys from local file content")

    # Log the API token for debugging
    logger.debug(f"Settings loaded with API_TOKEN: {settings.API_TOKEN[:5]}... (first 5 chars)")
    if settings.SOPS_AGE_PUBLIC_KEY:
        logger.debug(f"SOPS public key available: {settings.SOPS_AGE_PUBLIC_KEY[:10]}...")
    if settings.SOPS_AGE_PRIVATE_KEY:
        logger.debug("SOPS private key available")

    return settings


settings = _get_settings()
