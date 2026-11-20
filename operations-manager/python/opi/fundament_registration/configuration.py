"""The settings this endpoint needs, and which of them are missing.

Read once per request from :mod:`opi.core.config`, so a value that the operator fills in
later does not need a restart beyond the one the setting itself needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from opi.connectors.transip import TRANSIP_API_BASE
from opi.core.config import settings

#: TTL of the A record, in seconds.
RECORD_TTL = 300

#: How long a running attempt may take before the next question treats it as failed. A pod
#: that restarts mid-attempt leaves a row behind that nobody will ever finish.
ATTEMPT_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class FundamentConfiguration:
    """Everything an attempt needs, with the code left out: that one is compared, not used."""

    transip_token: str
    zone: str
    acme_directory: str
    acme_email: str
    sso_client_id: str
    sso_client_secret: str
    sso_discovery_url: str


@dataclass(frozen=True)
class VlamConfiguration:
    """The VLAM details an installation needs to reach the gateway."""

    login_server: str
    gateway: str
    api_host: str
    preauth_key: str


#: The setting behind every value, so a 503 can name the one that is missing.
_REQUIRED: dict[str, str] = {
    "transip_token": "FUNDAMENT_REGISTRATION_TRANSIP_TOKEN",
    "zone": "FUNDAMENT_REGISTRATION_ZONE",
    "acme_directory": "FUNDAMENT_REGISTRATION_ACME_DIRECTORY",
    "acme_email": "FUNDAMENT_REGISTRATION_ACME_EMAIL",
    "sso_client_id": "FUNDAMENT_REGISTRATION_SSO_CLIENT_ID",
    "sso_client_secret": "FUNDAMENT_REGISTRATION_SSO_CLIENT_SECRET",
    "sso_discovery_url": "FUNDAMENT_REGISTRATION_SSO_DISCOVERY_URL",
}


#: What the DNS hook needs: a subset of the table above. The hook is a process of its own
#: that reads these settings back from the environment :func:`hook_environment` describes,
#: so the other five of the table do not reach it and are not its business either.
_HOOK_REQUIRED: tuple[str, ...] = ("transip_token", "zone")

#: The variable that names the TransIP API the hook talks to, so its whole reach is in the
#: environment it is handed.
API_BASE_VARIABLE = "FUNDAMENT_REGISTRATION_TRANSIP_API_BASE"


@dataclass(frozen=True)
class HookConfiguration:
    """The values one DNS hook call runs on."""

    transip_token: str
    zone: str


def missing_settings() -> list[str]:
    """The names of the settings that are empty, in the order of the table above."""
    return [name for name in _REQUIRED.values() if not getattr(settings, name)]


def configuration() -> FundamentConfiguration:
    """The settings as one value. Only call this when :func:`missing_settings` is empty."""
    return FundamentConfiguration(**{field: getattr(settings, name) for field, name in _REQUIRED.items()})


def missing_hook_settings() -> list[str]:
    """The names of the settings the DNS hook misses."""
    return [_REQUIRED[field] for field in _HOOK_REQUIRED if not getattr(settings, _REQUIRED[field])]


def hook_configuration() -> HookConfiguration:
    """The hook's settings as one value. Only call this when :func:`missing_hook_settings` is empty."""
    return HookConfiguration(**{field: getattr(settings, _REQUIRED[field]) for field in _HOOK_REQUIRED})


def hook_environment(config: FundamentConfiguration) -> dict[str, str]:
    """The environment the DNS hook reads its own configuration back from.

    Both sides of the hook come off one table, so what lego hands it and what it refuses to
    run without cannot drift apart.
    """
    return {_REQUIRED[field]: getattr(config, field) for field in _HOOK_REQUIRED} | {
        API_BASE_VARIABLE: TRANSIP_API_BASE
    }


def vlam_configuration() -> VlamConfiguration | None:
    """The VLAM details, or None when the pre-auth key is not set.

    Only the tunnel: every user brings their own VLAM API key. Optional on purpose: a cluster
    without the key hands out an answer without a ``vlam``
    field, and the rest of the registration works as it does. Read per answer, so a replaced
    pre-auth key travels along with the next installation.
    """
    if not settings.FUNDAMENT_REGISTRATION_VLAM_PREAUTH_KEY:
        return None
    return VlamConfiguration(
        login_server=settings.FUNDAMENT_REGISTRATION_VLAM_LOGIN_SERVER,
        gateway=settings.FUNDAMENT_REGISTRATION_VLAM_GATEWAY,
        api_host=settings.FUNDAMENT_REGISTRATION_VLAM_API_HOST,
        preauth_key=settings.FUNDAMENT_REGISTRATION_VLAM_PREAUTH_KEY,
    )
