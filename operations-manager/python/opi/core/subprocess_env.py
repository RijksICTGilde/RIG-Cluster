"""The environment for child processes: `os.environ` without the platform secrets.

A child process that inherits the full environment also gets the platform key and every
admin password, readable in `/proc/<pid>/environ`. What git, kubectl, mc, kopia, pg_dump
or sops really needs, the caller passes in `extra`.
"""

from __future__ import annotations

import os

# The prefix also keeps out SOPS_AGE_KEY_FILE: sops reads that name itself and would then
# decrypt on the platform key without the caller passing anything. PROJECT_KEK_ is here for
# the same reason: it is the key that opens every project secret (RC-334).
PLATFORM_SECRET_PREFIXES: tuple[str, ...] = ("SOPS_AGE_", "PROJECT_KEK_")

# The full contents of the deployment env sources; a test fails on an unknown secretRef. What
# is not key material is kept out too: ADMIN_EMAILS and ALLOWED_EMAILS are personal data.
PLATFORM_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "ADMIN_EMAILS",
        "ALLOWED_EMAILS",
        "ARGOCD_PASSWORD",
        "ARGOCD_TOKEN",
        "BACKUP_S3_ACCESS_KEY",
        "BACKUP_S3_ENDPOINT",
        "BACKUP_S3_SECRET_KEY",
        "BACKUP_SWEEP_DRY_RUN",
        "DATABASE_ADMIN_PASSWORD",
        "DATABASE_APP_PASSWORD",
        "DATABASE_APP_USER",
        "DEBUG_MODE",
        "GRAFANA_TOKEN",
        "GRAFANA_URL",
        "KEYCLOAK_ADMIN_CLIENT_SECRET",
        "KEYCLOAK_ADMIN_PASSWORD",
        "KEYCLOAK_MASTER_OIDC_CLIENT_SECRET",
        "KEYCLOAK_OTP_ADMIN_PASSWORD",
        "KEYCLOAK_OTP_ADMIN_TOTP_SECRET",
        "KEYCLOAK_OTP_ADMIN_USERNAME",
        "LOGWATCHER_ENABLED",
        "LOGWATCHER_NTFY_TOPIC",
        "MAIL_RELAY_ADMIN_PASSWORD",
        "MAIL_RELAY_ADMIN_USERNAME",
        "METRICS_BACKEND",
        "MINIO_ADMIN_ACCESS_KEY",
        "MINIO_ADMIN_SECRET_KEY",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_DISCOVERY_URL",
        "OIDC_REALM",
        "OIDC_URL",
        "PROJECT_REPO_PASSWORD",
        "PROMETHEUS_METRICS_AUTH_TOKEN",
        "RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE",
        "REDIS_PASSWORD",
        "REGISTRY_PASSWORD",
        "SEAWEEDFS_ADMIN_ACCESS_KEY",
        "SEAWEEDFS_ADMIN_SECRET_KEY",
        "SECRET_KEY",
        "TRANSIP_ACCOUNT_NAME",
        "TRANSIP_PRIVATE_KEY",
        "WEED_JWT_FILER_SIGNING_KEY",
    }
)


def is_platform_secret(name: str) -> bool:
    return name in PLATFORM_SECRET_NAMES or name.startswith(PLATFORM_SECRET_PREFIXES)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment without the platform secrets, with `extra` on top."""
    env = {name: value for name, value in os.environ.items() if not is_platform_secret(name)}
    if extra:
        env.update(extra)
    return env
