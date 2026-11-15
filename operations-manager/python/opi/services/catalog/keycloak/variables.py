"""Environment variables the keycloak service provides -- single source of truth.

Lives in the service's own package: what a service hands to a deployment is part of that
service, not of a shared module.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class KeycloakVariables(Enum):
    """Keycloak/SSO service variable definitions - single source of truth."""

    CLIENT_ID = VariableDefinition(
        name="OIDC_CLIENT_ID",
        description="OAuth2/OIDC client identificatie voor authenticatie",
        source="secret",
        secret_key="client_id",  # noqa: S106  # the name of the key or the secret class, not a secret
    )
    CLIENT_SECRET = VariableDefinition(
        name="OIDC_CLIENT_SECRET",
        description="OAuth2/OIDC client geheim voor authenticatie",
        source="secret",
        secret_key="client_secret",  # noqa: S106  # the name of the key or the secret class, not a secret
    )
    PUBLIC_CLIENT_ID = VariableDefinition(
        name="OIDC_PUBLIC_CLIENT_ID",
        description="Public OAuth2/OIDC client identificatie voor browser-based authenticatie (keycloak-js)",
        source="secret",
        secret_key="public_client_id",  # noqa: S106  # the name of the key or the secret class, not a secret
    )
    DISCOVERY_URL = VariableDefinition(
        name="OIDC_DISCOVERY_URL",
        description="OIDC discovery endpoint URL voor configuratie",
        source="secret",
        secret_key="discovery_url",  # noqa: S106  # the name of the key or the secret class, not a secret
    )
    URL = VariableDefinition(
        name="OIDC_URL",
        description="Keycloak basis URL",
        source="secret",
        secret_key="base_url",  # noqa: S106  # the name of the key or the secret class, not a secret
    )
    REALM = VariableDefinition(
        name="OIDC_REALM",
        description="Keycloak realm naam",
        source="secret",
        secret_key="realm",  # noqa: S106  # the name of the key or the secret class, not a secret
    )
    HOSTNAME = VariableDefinition(
        name="OIDC_HOSTNAME",
        description="Keycloak hostname zonder scheme (afgeleid van base_url)",
        source="secret",
        secret_key="hostname",  # noqa: S106  # the name of the key or the secret class, not a secret
    )
