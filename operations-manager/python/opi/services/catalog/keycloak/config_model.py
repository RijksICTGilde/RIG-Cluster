"""Config model for the ``keycloak`` service (RC-5 Phase 2).

Keycloak is the polymorphic hard case the plan flags. Its config today is read and
heavily normalised in ``KeycloakManager._get_keycloak_service_config`` (hyphen ->
underscore keys, nested restrict-access, template-file existence check on disk, and
an entirely different key set when the service is external: ``host/realm/client-id/
client-secret``).

So v1.0 deliberately does the achievable part, not the hard 10%:
- It types and guardrails the common *internal* fields (template, redirect URIs,
  restrict-access, account-link) that users actually set.
- It keeps ``extra="allow"`` because the config is polymorphic: external keycloak
  carries a different key set, and ``additional-clients`` / ``realm-roles`` entries
  carry advanced pass-through keys. Forbidding extras would reject real files
  (e.g. the external ``type: external`` project). This mirrors today's permissive
  reader.
- The template-exists-on-disk check stays in the manager (it is I/O, not a data
  guardrail).

Keys use the on-disk spelling (mostly hyphenated) via aliases; ``populate_by_name``
lets both the alias and the Python name validate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AccountLink(StrEnum):
    AUTOMATIC = "automatic"
    CONFIRM = "confirm"
    VERIFY = "verify"


class RestrictAccessConfig(BaseModel):
    """``restrict-access`` sub-object; keys are well-defined so extras are forbidden."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    role: str | None = None
    realm_role: str | None = Field(default=None, alias="realm-role")
    error_message: str = Field(default="${accessDeniedNoPermission}", alias="error-message")


class KeycloakClientEntry(BaseModel):
    """An ``additional-clients`` item. Only ``name`` is guardrailed; the rest
    (redirect-uris, ...) is advanced pass-through -- the hard 10% the plan leaves
    hand-authored -- so extras are allowed."""

    model_config = ConfigDict(extra="allow")

    name: str


class KeycloakRealmRoleEntry(BaseModel):
    """A ``realm-roles`` item; ``name`` required, advanced keys pass through."""

    model_config = ConfigDict(extra="allow")

    name: str


class KeycloakRealm(BaseModel):
    """Per-realm admin connection (OPI-managed, one per cluster).

    Relocated verbatim from the old project-level ``config.keycloak`` (RC-5 B). Kept
    as-is -- matched by ``realm`` (deterministic ``{project}-{cluster}``) as before.
    ``extra="allow"`` tolerates fields written over time (e.g. ``service_client_secret``);
    password is stored AGE-encrypted or ``plain:``-prefixed, so it's just a string here.
    """

    model_config = ConfigDict(extra="allow")

    host: str
    realm: str
    username: str
    password: str
    # Shared TOTP seed for the realm-admin account (AGE-encrypted or ``plain:``-
    # prefixed, like ``password``). Only present when KEYCLOAK_ENFORCE_ADMIN_OTP
    # provisioned it; absent on pre-OTP realms.
    totp_secret: str | None = None


class KeycloakConfig(BaseModel):
    # extra="allow": config is polymorphic (internal template vs external
    # host/realm/client-id/client-secret) and must not reject real files.
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    template: str = "sso-only"
    # OPI-managed per-cluster admin connections, relocated from project config.keycloak.
    realms: list[KeycloakRealm] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    additional_redirect_uris: list[str] = Field(default_factory=list)
    restrict_access: RestrictAccessConfig | None = Field(default=None, alias="restrict-access")
    additional_clients: list[KeycloakClientEntry] = Field(default_factory=list, alias="additional-clients")
    realm_roles: list[KeycloakRealmRoleEntry] = Field(default_factory=list, alias="realm-roles")
    account_link: AccountLink | None = Field(default=None, alias="account-link")
