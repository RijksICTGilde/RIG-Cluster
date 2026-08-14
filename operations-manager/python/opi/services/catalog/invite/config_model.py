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

from pydantic import BaseModel, ConfigDict, Field

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

    key: str = Field(description="The secret in the invitation link; whoever holds it can redeem the invite.")
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
        default=None, alias="application-url", description="Where the user is sent after redeeming."
    )
    message: I18nText | None = Field(default=None, description="Text shown on the invitation page.")
    success_title: I18nText | None = Field(
        default=None, alias="success-title", description="Heading shown after a successful redemption."
    )
    success_button: I18nText | None = Field(
        default=None, alias="success-button", description="Label of the button leading to the application."
    )


class InviteConfig(BaseModel):
    """Project-level invite config: a default language and the list of active invites.

    ``settings`` is gone (it was a second level meaning the same as ``config``);
    ``default-language`` sits next to ``active``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: ``active`` is patchable entry by entry, keyed on the invite's own ``key``. Without
    #: it only the PUT existed, and the PUT wants every invite resent -- including the
    #: keys, which no read response gives back (they are the secret in the link). A
    #: second invite therefore cost the first one. See ``opi/services/config_lists.py``.
    ITEM_KEYS: ClassVar[dict[str, str | None]] = {"active": "key"}

    default_language: str = Field(
        default="nl",
        alias="default-language",
        description="Language the invitation page opens in when the visitor expresses no preference.",
    )
    active: list[InviteEntry] = Field(
        default_factory=list, description="The invitations that can currently be redeemed."
    )
