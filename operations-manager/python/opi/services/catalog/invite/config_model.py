"""Typed config model for the ``invite`` service (RC-13).

The project-level ``services/invite/config`` block is validated against this model
(convert-then-validate, like every other service). It replaces the old top-level
``invites:`` section and its hand-authored ``$defs/invites`` / ``$defs/invite`` /
``$defs/i18n-text`` in ``project_v2.json``.

Key spelling. Like the thirteen other services, the on-disk config keys are hyphenated
(``realm-roles``, ``restrict-domain``, ...). The model carries the hyphen aliases and
``populate_by_name=True`` so a file written with EITHER the hyphen alias (new, UI-created)
or the legacy underscore field name (the four production files that predate this service,
relocated verbatim by the schema migration) validates. The redemption flow reads invites
through the model at the ``ProjectFileHandler`` chokepoint (``extract_invites_config``),
which dumps with field names, so ``invite_manager`` / ``invite_routes`` keep reading the
underscore keys they already read -- no change to the public redemption surface.

``roles`` and ``realm_roles`` do the same thing: ``assign_invite_permissions``
(``opi/manager/invite_manager.py``) merges both into one realm-role assignment. Both are
kept so existing files keep validating; the UI only offers ``realm_roles`` and ``roles``
is documented here as deprecated.

``groups`` and ``client_roles`` are advanced pass-through: none of the four live projects
use them and the UI does not offer them, but they stay in the model so hand-authored YAML
keeps validating (like ``KeycloakClientEntry``'s pass-through fields).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opi.services.catalog.invite.target_format import split_target

#: The two authentication methods an invite can offer. A closed set, so it is typed as a
#: Literal in the model (the guardrail) rather than relying on the form widget's options.
AuthMethod = Literal["sso", "local"]


class I18nText(BaseModel):
    """A ``{nl, en}`` translated string. Replaces ``$defs/i18n-text``."""

    model_config = ConfigDict(extra="forbid")

    nl: str | None = Field(default=None, description="Dutch text.")
    en: str | None = Field(default=None, description="English text.")


class InviteEntry(BaseModel):
    """One invitation. ``key`` is the shared-link secret; everything else describes what
    the redeeming user gets in the project's Keycloak realm."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str = Field(
        description=(
            "The secret in the invitation link; whoever holds it can redeem the invite. Send an empty "
            "string to have a random one generated -- the write reports it back under 'generated', "
            "which is the only place you learn a key you did not choose. It is RETURNED by a read of "
            "this config, deliberately: the code is the invitation, so whoever cannot read it back "
            "cannot send it on."
        )
    )
    roles: list[str] = Field(
        default_factory=list,
        description="Deprecated spelling of 'realm-roles', kept so existing files validate. Use realm-roles.",
    )
    realm_roles: list[str] = Field(
        default_factory=list, alias="realm-roles", description="Realm roles the redeeming user is given."
    )
    client_roles: dict[str, list[str]] = Field(
        default_factory=dict,
        alias="client-roles",
        description="Per client, the client roles the redeeming user is given. Advanced; not offered in the portal.",
    )
    groups: list[str] = Field(
        default_factory=list,
        description="Realm groups the redeeming user joins. Advanced; not offered in the portal.",
    )
    restrict_domain: str | None = Field(
        default=None,
        alias="restrict-domain",
        description="Only accept an email address in this domain, so the link cannot be passed on freely.",
    )
    auth_methods: list[AuthMethod] = Field(
        default_factory=list,
        alias="auth-methods",
        description="The sign-in methods this invite offers; empty means every method the realm supports.",
    )
    contact_email: str | None = Field(
        default=None, alias="contact-email", description="Address shown to a user who needs help redeeming."
    )
    application_url: str | None = Field(
        default=None,
        alias="application-url",
        description=(
            "Where the user is sent after redeeming, as a fixed address. Use this for a destination "
            "OUTSIDE this project; for one of the project's own addresses use 'application-target', "
            "which keeps following it when the subdomain or the domain format changes."
        ),
    )
    application_target: str | None = Field(
        default=None,
        alias="application-target",
        description=(
            "Where the user is sent after redeeming, as the CHOICE behind the address: "
            "'component:deployment', or 'component:deployment:/path' where the component publishes "
            "more than one path. The address is worked out when the page is rendered, so it follows a "
            "subdomain or domain-format change. Mutually exclusive with 'application-url'."
        ),
    )
    title: I18nText | None = Field(
        default=None,
        description=(
            "What the invite gives access to, as the invitee knows it (for example 'Docs en Grist'). "
            "Heading of the invitation pages; empty falls back to the project's display name."
        ),
    )
    message: I18nText | None = Field(default=None, description="Text shown on the invitation page.")
    success_title: I18nText | None = Field(
        default=None, alias="success-title", description="Heading shown after a successful redemption."
    )
    success_button: I18nText | None = Field(
        default=None, alias="success-button", description="Label of the button leading to the application."
    )

    @model_validator(mode="after")
    def _the_target_names_a_component_and_a_deployment(self) -> InviteEntry:
        """The composite value has to split into two names, or it names nothing.

        Without this a typo is accepted and turns into "no button" at render time, which
        looks exactly like a destination someone deliberately left empty. Rejecting it here
        means the API and the CLI say so at write time, where the typo can still be fixed.
        The PATH is not checked: it is free-form and only has to match what the component
        publishes, which this model cannot see.

        The rejected value is NOT echoed. This message reaches a user through
        ``validation_reasons``, and a value someone got wrong may be one they pasted from
        somewhere else -- the same reason the config-validation chokepoint stopped quoting
        pydantic's ``input_value``.
        """
        if self.application_target is None:
            return self
        component, deployment, _path = split_target(self.application_target)
        if not component or not deployment:
            msg = (
                "'application-target' noemt een component en een deployment, gescheiden door een "
                "dubbele punt: 'component:deployment', of 'component:deployment:/pad' als de "
                "component meer dan een pad publiceert"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _one_destination_at_most(self) -> InviteEntry:
        """At most one destination: a fixed address OR a deployment/component choice.

        Neither is also fine -- an invitation without a destination simply shows no button,
        and that is a valid thing to want. Both is not: they can point at two different
        places and nothing decides which one wins, so the reader would have to guess.
        """
        if self.application_url and self.application_target:
            msg = (
                "een uitnodiging heeft één bestemming: kies 'application-target' (een deployment "
                "en component van dit project) of 'application-url' (een vast adres), niet allebei"
            )
            raise ValueError(msg)
        return self


class InviteConfig(BaseModel):
    """Project-level invite config: a default language and the list of active invites.

    ``settings`` is gone (it was a second level meaning the same as ``config``);
    ``default-language`` sits next to ``active``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: ``active`` is patchable entry by entry, keyed on the invite's own ``key``. Without
    #: it only the PUT existed, and the PUT wants every invite resent, so a second invite
    #: cost the first one. The keys ARE readable (see the module docstring of
    #: ``opi/services/catalog/invite``), so resending is possible in principle -- but a
    #: read-modify-write over a whole list is still how entries get lost, and that is what
    #: the PATCH is for. See ``opi/services/config_lists.py``.
    ITEM_KEYS: ClassVar[dict[str, str | None]] = {"active": "key"}

    default_language: str = Field(
        default="nl",
        alias="default-language",
        description="Language the invitation page opens in when the visitor expresses no preference.",
    )
    active: list[InviteEntry] = Field(
        default_factory=list,
        description=(
            "The invitations that can currently be redeemed. Over the API this is presented as "
            "ONE entry rather than a list, because in practice there is one; add a second with the "
            "PATCH on this field. Roles go in 'realm-roles' -- 'roles' is the older spelling of the "
            "same thing and only still exists so older project files keep validating."
        ),
    )
