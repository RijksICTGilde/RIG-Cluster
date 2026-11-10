"""Config model for the ``keycloak`` service.

This config is polymorphic, which is why the model is as loose as it is. The reading and
normalisation (hyphen -> underscore keys, nested restrict-access, the template-file
existence check on disk) stay in ``KeycloakManager._get_keycloak_service_config``: that last
one is I/O, not a data guardrail.

What the model does type and guardrail are the common *internal* fields (template, redirect
URIs, restrict-access, account-link). ``extra="allow"`` stays: an external keycloak carries a
different key set (``host/realm/client-id/client-secret``) and ``additional-clients`` /
``realm-roles`` entries carry advanced pass-through keys, so forbidding extras rejects real
project files.

Keys use the on-disk spelling (mostly hyphenated) via aliases; ``populate_by_name``
lets both the alias and the Python name validate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from opi.services.catalog.keycloak.templates import SSO_ONLY_TEMPLATE
from opi.services.config_managed import PLATFORM_MANAGED
from opi.utils.age import AGE_ENCRYPTED_PATTERN
from opi.utils.redirect_uris import validate_redirect_uris
from opi.utils.url_paths import normalize_logout_path


class AccountLink(StrEnum):
    """The settings that DO something.

    ``keycloak_yaml_handler`` builds a first-broker-login flow of its own for ``automatic``
    and ``confirm`` only; every other value leaves Keycloak's own flow in place, which is
    exactly what choosing nothing gives.
    """

    AUTOMATIC = "automatic"
    CONFIRM = "confirm"


#: The dropped setting, as it appears in existing project files.
LEGACY_ACCOUNT_LINK = "verify"


#: The requirement "switching it on only works WITH a role", expressed in standard JSON
#: Schema.
#:
#: Both halves out of one model: the ``anyOf`` describes the rule for whoever reads the schema
#: (a ``model_validator`` does not reach the generated schema and therefore not the OpenAPI
#: document), the validator enforces it for whoever ignores it.
#:
#: The branches, in order: ``enabled`` is off (or absent, where the default False applies), or
#: there is a non-empty ``role``, or there is a non-empty ``realm-role``.
ROLE_REQUIRED_WHEN_ENABLED: dict[str, Any] = {
    "anyOf": [
        {"properties": {"enabled": {"const": False}}},
        {"required": ["role"], "properties": {"role": {"type": "string", "minLength": 1}}},
        {"required": ["realm-role"], "properties": {"realm-role": {"type": "string", "minLength": 1}}},
    ]
}


class RestrictAccessConfig(BaseModel):
    """``restrict-access`` sub-object; keys are well-defined so extras are forbidden.

    Enabling the restriction without naming a role is refused here, so the rule lives in
    one place: the model. See ``ROLE_REQUIRED_WHEN_ENABLED`` for why it is also written
    out in the schema.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, json_schema_extra=ROLE_REQUIRED_WHEN_ENABLED)

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

    @model_validator(mode="after")
    def _role_required_when_enabled(self) -> RestrictAccessConfig:
        """The rule itself. The text is the deploy error's, so whoever knows it recognises it."""
        if self.enabled and not self.role and not self.realm_role:
            raise ValueError(
                "restrict-access.role or restrict-access.realm-role is required when restrict-access.enabled is True"
            )
        return self


class KeycloakClientEntry(BaseModel):
    """An ``additional-clients`` item with guardrailed ``name`` and ``redirect-uris``.

    Pass-through keys are allowed for advanced configurations."""

    # No ``populate_by_name``, unlike on ``KeycloakConfig``: only the hyphenated spelling
    # validates, because that is the key ``_create_additional_clients`` reads.
    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Client id of the extra client to create in the realm.")
    redirect_uris: list[str] = Field(
        alias="redirect-uris",
        min_length=1,
        description="Callback URLs this client accepts; https:// with a concrete host, or http:// to a loopback address.",
    )
    direct_access_grants: bool = Field(
        default=False,
        alias="direct-access-grants",
        description="Allow the password grant (ROPC) on this client. Only set it when the application has no browser.",
    )
    service_accounts: bool = Field(
        default=False,
        alias="service-accounts",
        description="Allow this client to get a token for itself (client credentials).",
    )

    @field_validator("redirect_uris")
    @classmethod
    def _validate_redirect_uris(cls, value: list[str]) -> list[str]:
        return validate_redirect_uris(value, "additional-clients.redirect-uris")


class KeycloakRealmRoleEntry(BaseModel):
    """A ``realm-roles`` item; ``name`` required, advanced keys pass through."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Name of the realm role to create.")


class KeycloakRealm(BaseModel):
    """Per-realm admin connection (OPI-managed, one per cluster).

    Matched by ``realm`` (deterministic ``{project}-{cluster}``). ``extra="allow"`` tolerates
    fields written over time (e.g. ``service_client_secret``).
    """

    model_config = ConfigDict(extra="allow")

    host: str = Field(description="Keycloak base URL this realm lives on. Written by the platform.")
    realm: str = Field(description="Realm name, {project}-{cluster}. Written by the platform.")
    username: str = Field(description="Realm-admin account the platform manages the realm with.")
    password: str = Field(
        pattern=AGE_ENCRYPTED_PATTERN,
        description="That account's password, AGE-encrypted.",
    )
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
        default=SSO_ONLY_TEMPLATE,
        description=(
            "Realm template the realm is created from: 'sso-only' is single sign-on without extras, "
            "'sso-support' adds a login screen where a local account can sign in, and 'sso-support-otp' "
            "also requires an OTP from such a local account."
        ),
    )
    realms: list[KeycloakRealm] = Field(
        default_factory=list,
        # Platform-written, and the ONLY place the realm-admin password lives. A PUT that
        # leaves the field out carries the stored value over. See
        # opi/services/config_managed.py.
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Per-cluster realm admin connections. Written and managed by the platform: it is carried over "
            "on a write, so a caller neither has to send it nor can lose it by leaving it out."
        ),
    )
    variables: dict[str, Any] = Field(
        default_factory=dict, description="Values filled into the realm template's placeholders."
    )
    additional_redirect_uris: list[str] = Field(
        default_factory=list,
        # The only compound field here without a hyphenated spelling; all its siblings
        # (restrict-access, additional-clients, realm-roles, account-link) have one. Two
        # spellings mixed in the same block is exactly where somebody types the other one some
        # day, and ``extra="allow"`` swallows that silently.
        #
        # Both are therefore read, and the python path stays leading: the field NAME does not
        # change, so existing files stay as they are and the API keeps naming the same key. A
        # migration that rewrites files does not belong here.
        validation_alias=AliasChoices("additional_redirect_uris", "additional-redirect-uris"),
        description="Extra redirect URIs the client accepts, on top of the deployment URLs (e.g. localhost for development).",
    )
    direct_access_grants: bool = Field(
        default=False,
        alias="direct-access-grants",
        description=(
            "Allow the password grant (ROPC) on the deployment client. Only set it when the application has no "
            "browser to send the user to Keycloak with."
        ),
    )
    service_accounts: bool = Field(
        default=False,
        alias="service-accounts",
        description="Allow the deployment client to get a token for itself (client credentials).",
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
    logout_path: str | None = Field(
        default=None,
        alias="logout-path",
        description=(
            "Path on the component that ends its own session, relative to the deployment's own host "
            "(e.g. /api/v1.0/logout/). Keycloak calls it as this client's front-channel logout URL. "
            "Leave it out when the application already clears its session before it sends the browser "
            "to the end-session endpoint."
        ),
    )
    account_link: AccountLink | None = Field(
        default=None,
        alias="account-link",
        description=(
            "How an existing account is linked when a user signs in through an identity provider: "
            "automatic or confirm. Leave it out for Keycloak's own flow (verification by email)."
        ),
    )

    @field_validator("additional_redirect_uris")
    @classmethod
    def _validate_extra_redirect_uris(cls, value: list[str]) -> list[str]:
        """The same rule as on the extra clients; the form reads it through the same function."""
        return validate_redirect_uris(value, "additional_redirect_uris")

    @field_validator("logout_path", mode="before")
    @classmethod
    def _normalize_logout_path(cls, value: Any) -> Any:
        """One normalisation for this field; ``KeycloakManager`` reads it through the same function."""
        return normalize_logout_path(value)

    @field_validator("account_link", mode="before")
    @classmethod
    def _allow_legacy_account_link(cls, value: Any) -> Any:
        """An existing project file with ``account-link: verify`` stays processable.

        ``verify`` is not in the enum: it did the same as choosing nothing, since both land on
        Keycloak's own flow. Hard-refusing the stored value would be the DANGEROUS part -- a
        value that no longer validates blocks every further processing of that project, and
        silently, because nobody looks at a project that was running fine.

        It is therefore read as "nothing chosen". The project file is not rewritten here; on
        the next save through the form the value disappears by itself, because the option list
        does not know it.
        """
        return None if value == LEGACY_ACCOUNT_LINK else value
