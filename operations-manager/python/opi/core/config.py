import logging
import os
import pathlib

from pydantic_settings import BaseSettings

# Initialize logging early to ensure it's available during config loading
from opi.core.early_logging import initialize_logging  # noqa: F401
from opi.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)

PROJECT_NAME: str = "OPI"
VERSION: str = "0.1.0"  # replace in CI/CD pipeline
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
                if line.startswith("ENVIRONMENT=") or line.startswith("ENVIRONMENT "):
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
            logger.error(f"Required environment file missing: {env_specific} (ENVIRONMENT={environment_var})")
            logger.error(f"Create {env_specific} or remove '{environment}' from ENVIRONMENT variable")

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
    model_config = {"env_file": _get_env_files(), "env_file_encoding": "utf-8"}

    OWN_DOMAIN: str = "operations-manager.kind"

    SECRET_KEY: str = "default-secret-key-for-development-change-in-production"
    ENVIRONMENT: str = "local"
    DEBUG: bool = True
    CLUSTER_MANAGER: str = "local"

    # Developer settings
    FIXED_PROJECT_POSTFIX: str | None = None  # If set, use this instead of random postfix for project names
    ALLOW_PROJECTFILES_OVERWRITE: bool = False  # If True, allow overwriting existing project files
    RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE: bool = (
        False  # If True, recreate passwords/users when authentication fails
    )

    # Legacy Git server settings (for backward compatibility)
    # TODO: This is only used for testing creating local GIT repositories and should be removed or fixed in the future
    GIT_SERVER_HOST: str = "localhost"
    GIT_SERVER_PORT: int = 2222
    GIT_SERVER_KEY_PATH: str = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keys/git-server-key"
    GIT_SERVER_USER: str = "git"

    # OIDC settings
    OIDC_CLIENT_ID: str | None = None
    OIDC_CLIENT_SECRET: str | None = None
    OIDC_DISCOVERY_URL: str | None = None

    # Git projects server settings - for monitoring and retrieving project files
    ENABLE_GIT_MONITOR: bool = False
    GIT_PROJECTS_SERVER_URL: str = "git://localhost:9090/"
    GIT_PROJECTS_SERVER_USERNAME: str | None = None  # Username for Git projects server authentication
    GIT_PROJECTS_SERVER_PASSWORD: str | None = None  # Password for Git projects server (can be SOPS encrypted)
    GIT_PROJECTS_SERVER_REPO_PATH: str = "/"
    GIT_PROJECTS_SERVER_FILE_PATH: str = "projects/simple-example.yaml"
    GIT_PROJECTS_SERVER_BRANCH: str = "main"
    GIT_PROJECTS_SERVER_POLL_INTERVAL: int = 120  # seconds

    # ArgoCD Applications Git repository - simplified to just URL and credentials
    GIT_ARGO_APPLICATIONS_URL: str = "ssh://git@localhost:2222/srv/git/argo-applications.git"
    GIT_ARGO_APPLICATIONS_KEY: str = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keys/git-server-key"
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

    # SOPS age key settings (from Kubernetes secret)
    SOPS_AGE_KEY_CONTENT: str | None = None  # Full SOPS age key content from secret
    SOPS_AGE_PUBLIC_KEY: str | None = None  # Public key for SOPS age encryption
    SOPS_AGE_PRIVATE_KEY: str | None = None  # Private key for SOPS age decryption

    # Logging configuration
    LOG_TO_FILE: bool = False  # Enable file logging alongside stdout
    LOG_FILE_PATH: str = "log.txt"  # Path to log file when LOG_TO_FILE is enabled

    # Temporary directory configuration
    TEMP_DIR: str = "/tmp"  # Default temp directory, can be overridden by TMPDIR env var

    # Keycloak configuration
    KEYCLOAK_URL: str = "http://keycloak.kind"
    KEYCLOAK_ADMIN_USERNAME: str = "admin"
    KEYCLOAK_ADMIN_PASSWORD: str = "changeMe123!"

    # Default shared realm configuration
    KEYCLOAK_DEFAULT_REALM: str = "rig-platform"
    KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME: str = "RIG Platform"

    # Master OIDC provider configuration (to be added to shared realm)
    KEYCLOAK_MASTER_OIDC_CLIENT_ID: str = "dummy-client-id"
    KEYCLOAK_MASTER_OIDC_CLIENT_SECRET: str = "dummy-client-secret-123"
    KEYCLOAK_MASTER_OIDC_DISCOVERY_URL: str = "http://keycloak.kind/realms/master/.well-known/openid-configuration"

    # Database configuration
    DATABASE_HOST: str = "postgresql.kind"
    DATABASE_ADMIN_NAME: str = "postgres"
    DATABASE_ADMIN_PASSWORD: str = "changeMe123!"

    # MinIO configuration
    MINIO_HOST: str = "minio.kind:9000"
    MINIO_ADMIN_ACCESS_KEY: str = "admin"
    MINIO_ADMIN_SECRET_KEY: str = "changeMe123!"
    MINIO_USE_TLS: bool = False
    MINIO_REGION: str = "us-east-1"  # AWS region for S3 compatibility


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
            logger.debug(f"Private key: {private_key[:20]}...")

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

    setup_logging(log_to_file=settings.LOG_TO_FILE, log_file_path=settings.LOG_FILE_PATH)

    # Detailed logging for SOPS key configuration
    logger.info("=== SOPS Age Key Configuration Debug ===")

    logger.info(f"Environment SOPS_AGE_KEY_CONTENT: {'SET' if os.environ.get('SOPS_AGE_KEY_CONTENT') else 'NOT SET'}")
    if os.environ.get("SOPS_AGE_KEY_CONTENT"):
        content_length = len(os.environ.get("SOPS_AGE_KEY_CONTENT", ""))
        logger.info(f"SOPS_AGE_KEY_CONTENT length: {content_length} characters")
        logger.debug(
            f"SOPS_AGE_KEY_CONTENT preview (first 100 chars): {os.environ.get('SOPS_AGE_KEY_CONTENT', '')[:100]}..."
        )

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
            logger.info(
                f"Parsed private key: {private_key[:25]}...{private_key[-10:] if len(private_key) > 35 else ''}"
            )

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
                logger.info(
                    f"Parsed private key: {private_key[:25]}...{private_key[-10:] if len(private_key) > 35 else ''}"
                )

            logger.info("Successfully parsed SOPS age keys from local file content")

    # Log the API token for debugging
    logger.debug(f"Settings loaded with API_TOKEN: {settings.API_TOKEN[:5]}... (first 5 chars)")
    if settings.SOPS_AGE_PUBLIC_KEY:
        logger.debug(f"SOPS public key available: {settings.SOPS_AGE_PUBLIC_KEY[:10]}...")
    if settings.SOPS_AGE_PRIVATE_KEY:
        logger.debug("SOPS private key available")

    return settings


settings = _get_settings()
