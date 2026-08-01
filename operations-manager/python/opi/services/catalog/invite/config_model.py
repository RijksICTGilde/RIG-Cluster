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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The two authentication methods an invite can offer. A closed set, so it is typed as a
#: Literal in the model (the guardrail) rather than relying on the form widget's options.
AuthMethod = Literal["sso", "local"]


class I18nText(BaseModel):
    """A ``{nl, en}`` translated string. Replaces ``$defs/i18n-text``."""

    model_config = ConfigDict(extra="forbid")

    nl: str | None = None
    en: str | None = None


class InviteEntry(BaseModel):
    """One invitation. ``key`` is the shared-link secret; everything else describes what
    the redeeming user gets in the project's Keycloak realm."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str
    #: Deprecated. Same effect as ``realm_roles`` (both are merged in assign_invite_permissions);
    #: kept so existing files validate. The UI offers ``realm_roles`` only.
    roles: list[str] = Field(default_factory=list)
    realm_roles: list[str] = Field(default_factory=list, alias="realm-roles")
    #: Advanced pass-through (not offered in the UI): per-client role assignments.
    client_roles: dict[str, list[str]] = Field(default_factory=dict, alias="client-roles")
    #: Advanced pass-through (not offered in the UI): realm groups to join.
    groups: list[str] = Field(default_factory=list)
    restrict_domain: str | None = Field(default=None, alias="restrict-domain")
    auth_methods: list[AuthMethod] = Field(default_factory=list, alias="auth-methods")
    contact_email: str | None = Field(default=None, alias="contact-email")
    application_url: str | None = Field(default=None, alias="application-url")
    message: I18nText | None = None
    success_title: I18nText | None = Field(default=None, alias="success-title")
    success_button: I18nText | None = Field(default=None, alias="success-button")


class InviteConfig(BaseModel):
    """Project-level invite config: a default language and the list of active invites.

    ``settings`` is gone (it was a second level meaning the same as ``config``);
    ``default-language`` sits next to ``active``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_language: str = Field(default="nl", alias="default-language")
    active: list[InviteEntry] = Field(default_factory=list)
