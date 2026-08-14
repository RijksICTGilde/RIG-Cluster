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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class AccountLink(StrEnum):
    """De standen die iets DOEN.

    Hier stond ook ``VERIFY``, en die deed niets: ``keycloak_yaml_handler`` bouwt alleen
    voor ``automatic`` en ``confirm`` een eigen first-broker-login-flow en laat de rest op
    Keycloaks eigen flow staan - precies wat je krijgt als je niets kiest. Wie ``verify``
    koos kreeg dus dezelfde uitkomst als wie het veld leeg liet, terwijl de keuzelijst
    deed alsof er een verschil was.
    """

    AUTOMATIC = "automatic"
    CONFIRM = "confirm"


#: De weggevallen stand, zoals hij in bestaande projectbestanden staat.
LEGACY_ACCOUNT_LINK = "verify"


#: De eis "aan zetten kan alleen mét een rol", uitgedrukt in standaard JSON Schema.
#:
#: Diezelfde eis stond alleen in ``KeycloakManager._get_keycloak_service_config`` en sloeg
#: dus pas toe bij de UITROL, terwijl een client die het schema leest niets zag dat hem
#: tegenhield. Een ``model_validator`` alleen repareert dat half: die bewaakt de API, maar
#: verschijnt niet in het gegenereerde schema en dus niet in het OpenAPI-document. Beide
#: dus, uit één model: de ``anyOf`` beschrijft de regel voor wie hem leest, de validator
#: dwingt hem af voor wie hem negeert.
#:
#: De takken, op volgorde: ``enabled`` staat uit (of ontbreekt, dan geldt de default False),
#: of er staat een niet-lege ``role``, of er staat een niet-lege ``realm-role``.
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
    def _rol_verplicht_als_ingeschakeld(self) -> RestrictAccessConfig:
        """De regel zelf. De tekst is die van de uitrolfout, zodat wie hem kent hem herkent."""
        if self.enabled and not self.role and not self.realm_role:
            raise ValueError(
                "restrict-access.role or restrict-access.realm-role is required when restrict-access.enabled is True"
            )
        return self


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
            "automatic or confirm. Leave it out for Keycloak's own flow (verification by email)."
        ),
    )

    @field_validator("account_link", mode="before")
    @classmethod
    def _laat_de_oude_stand_door(cls, waarde: Any) -> Any:
        """Een bestaand projectbestand met ``account-link: verify`` blijft verwerkbaar.

        ``verify`` is uit de enum gehaald omdat hij niets deed. Hem daarna hard afkeuren
        zou het GEVAARLIJKE deel zijn: een waarde die niet meer valideert blokkeert elke
        volgende verwerking van dat project, en dat faalt stil - niemand kijkt naar een
        project dat prima stond te draaien.

        Hij wordt dus gelezen als "niets gekozen", en dat is geen interpretatie maar
        precies wat hij deed: beide standen komen op de eigen flow van Keycloak uit. Het
        projectbestand wordt hier niet herschreven; bij de eerstvolgende opslag via het
        formulier verdwijnt de waarde vanzelf, want de keuzelijst kent hem niet meer.
        """
        return None if waarde == LEGACY_ACCOUNT_LINK else waarde
