"""De omgeving voor kindprocessen: `os.environ` zonder de geheimen van het platform.

Elke git, kubectl, mc, kopia, pg_dump en sops die OPI start erfde de volledige omgeving,
en daarmee de platformsleutel en elk beheerwachtwoord, zichtbaar in `/proc/<pid>/environ`.
Wat een kindproces echt nodig heeft, geeft de aanroeper mee via `extra`.
"""

from __future__ import annotations

import os

# De prefix weert ook SOPS_AGE_KEY_FILE: sops leest die naam zelf en zou dan op de
# platformsleutel ontsleutelen zonder dat de aanroeper iets meegaf. PROJECT_KEK_ staat er
# om dezelfde reden: dat is de sleutel die elk projectgeheim opent (RC-334).
PLATFORM_SECRET_PREFIXES: tuple[str, ...] = ("SOPS_AGE_", "PROJECT_KEK_")

# Volledige inhoud van deployment env-bronnen; toets faalt op onbekende secretRef. Ook wat
# geen sleutelmateriaal is erft niet mee: ADMIN_EMAILS en ALLOWED_EMAILS zijn persoonsgegevens.
PLATFORM_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "ADMIN_EMAILS",
        "ALLOWED_EMAILS",
        "ARGOCD_PASSWORD",
        "BACKUP_S3_ACCESS_KEY",
        "BACKUP_S3_ENDPOINT",
        "BACKUP_S3_SECRET_KEY",
        "BACKUP_SWEEP_DRY_RUN",
        "DATABASE_ADMIN_PASSWORD",
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
        "PROJECT_REPO_PASSWORD",
        "PROMETHEUS_METRICS_AUTH_TOKEN",
        "RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE",
        "REDIS_PASSWORD",
        "REGISTRY_PASSWORD",
        "SECRET_KEY",
        "TRANSIP_ACCOUNT_NAME",
        "TRANSIP_PRIVATE_KEY",
    }
)


def is_platform_secret(name: str) -> bool:
    """Draagt deze omgevingsnaam een geheim van het platform."""
    return name in PLATFORM_SECRET_NAMES or name.startswith(PLATFORM_SECRET_PREFIXES)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """De omgeving van dit proces zonder platformgeheimen, aangevuld met `extra`."""
    env = {name: value for name, value in os.environ.items() if not is_platform_secret(name)}
    if extra:
        env.update(extra)
    return env
