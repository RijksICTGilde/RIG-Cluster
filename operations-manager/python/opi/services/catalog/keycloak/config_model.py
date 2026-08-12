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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class AccountLink(StrEnum):
    AUTOMATIC = "automatic"
    CONFIRM = "confirm"
    VERIFY = "verify"


class RestrictAccessConfig(BaseModel):
    """``restrict-access`` sub-object; keys are well-defined so extras are forbidden."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = Field(default=False, description="Whether sign-in is limited to users holding the role below.")
    role: str | None = Field(default=None, description="Client role a user must hold to be let in.")
    realm_role: str | None = Field(
        default=None, alias="realm-role", description="Realm role a user must hold to be let in."
    )
    error_message: str = Field(
        default="${accessDeniedNoPermission}",
        alias="error-message",
        description="Message shown to a user without the role; a ${...} value is a Keycloak message key.",
    )


class KeycloakClientEntry(BaseModel):
    """An ``additional-clients`` item. Only ``name`` is guardrailed; the rest
    (redirect-uris, ...) is advanced pass-through -- the hard 10% the plan leaves
    hand-authored -- so extras are allowed."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Client id of the extra client to create in the realm.")


class KeycloakRealmRoleEntry(BaseModel):
    """A ``realm-roles`` item; ``name`` required, advanced keys pass through."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Name of the realm role to create.")


class KeycloakRealm(BaseModel):
    """Per-realm admin connection (OPI-managed, one per cluster).

    Relocated verbatim from the old project-level ``config.keycloak`` (RC-5 B). Kept
    as-is -- matched by ``realm`` (deterministic ``{project}-{cluster}``) as before.
    ``extra="allow"`` tolerates fields written over time (e.g. ``service_client_secret``);
    password is stored AGE-encrypted or ``plain:``-prefixed, so it's just a string here.
    """

    model_config = ConfigDict(extra="allow")

    host: str = Field(description="Keycloak base URL this realm lives on. Written by the platform.")
    realm: str = Field(description="Realm name, {project}-{cluster}. Written by the platform.")
    username: str = Field(description="Realm-admin account the platform manages the realm with.")
    password: str = Field(description="That account's password, AGE-encrypted or 'plain:'-prefixed.")
    totp_secret: str | None = Field(
        default=None,
        description=(
            "Shared TOTP seed for the realm-admin account, stored like the password. Only present when the "
            "cluster provisions admin OTP."
        ),
    )


class KeycloakConfig(BaseModel):
    # extra="allow": config is polymorphic (internal template vs external
    # host/realm/client-id/client-secret) and must not reject real files.
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    template: str = Field(
        default="sso-only",
        description="Realm template the realm is created from; 'sso-only' is single sign-on without extras.",
    )
    realms: list[KeycloakRealm] = Field(
        default_factory=list,
        description="Per-cluster realm admin connections. Written and managed by the platform.",
    )
    variables: dict[str, Any] = Field(
        default_factory=dict, description="Values filled into the realm template's placeholders."
    )
    additional_redirect_uris: list[str] = Field(
        default_factory=list,
        # Het enige samengestelde veld hier zonder koppelteken-schrijfwijze; alle broers
        # en zussen (restrict-access, additional-clients, realm-roles, account-link)
        # hebben er wel een. Twee schrijfwijzen door elkaar in hetzelfde blok is precies
        # waar iemand een keer de andere typt, en ``extra="allow"`` slikt die stil.
        #
        # Beide worden dus gelezen, en het pythonpad blijft leidend: de veldNAAM
        # verandert niet, dus bestaande bestanden blijven zoals ze zijn en de API blijft
        # dezelfde sleutel noemen. Een migratie die bestanden herschrijft hoort hier niet.
        validation_alias=AliasChoices("additional_redirect_uris", "additional-redirect-uris"),
        description="Extra redirect URIs the client accepts, on top of the deployment URLs (e.g. localhost for development).",
    )
    restrict_access: RestrictAccessConfig | None = Field(
        default=None, alias="restrict-access", description="Limit sign-in to users holding a given role."
    )
    additional_clients: list[KeycloakClientEntry] = Field(
        default_factory=list, alias="additional-clients", description="Extra Keycloak clients to create in the realm."
    )
    realm_roles: list[KeycloakRealmRoleEntry] = Field(
        default_factory=list, alias="realm-roles", description="Realm roles to create."
    )
    account_link: AccountLink | None = Field(
        default=None,
        alias="account-link",
        description=(
            "How an existing account is linked when a user signs in through an identity provider: "
            "automatic, confirm or verify."
        ),
    )
