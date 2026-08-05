"""Editable definitions for the invite service (project-level config).

All fields live under ``services/invite/config``. Paths are built with ``config_path``
so the layer/service is encoded once (enum-driven) instead of a hardcoded string. Every
editable carries ``virtualize=("services", "_services-config")`` so project-level service
config does not collide with the service-selection list in the wizard state.

The ``active`` sequence holds one item per invitation; its children are the invite fields.
The realm-roles picker is a sequence of selects fed by ``InviteRealmRoleOptionsProvider``
(reads the keycloak config from the surrounding form data). ``groups`` and ``client_roles``
are deliberately NOT offered here (advanced pass-through, see the config model).
"""

from __future__ import annotations

from opi.forms.editables.converters import EmptyToNoneConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import (
    AllowedValuesValidator,
    EmailValidator,
    InviteKeyValidator,
    RealmRoleValidator,
    UrlValidator,
)
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.invite.defaults import (
    default_contact_email,
    default_message_en,
    default_message_nl,
    default_success_button_en,
    default_success_button_nl,
    default_success_title_en,
    default_success_title_nl,
)
from opi.services.services_enums import ServiceType

_VIRTUALIZE = ("services", "_services-config")


def _cp(*segments: str) -> str:
    return config_path(ConfigLayer.PROJECT, ServiceType.INVITE, "config", *segments)


# --- project-level -----------------------------------------------------------

INVITE_DEFAULT_LANGUAGE_EDITABLE = Editable(
    yaml_path=_cp("default-language"),
    values_provider="InviteLanguageOptionsProvider",
    validator=AllowedValuesValidator(["nl", "en"]),
    default="nl",
    virtualize=_VIRTUALIZE,
)

# --- per-invite item fields --------------------------------------------------

# Optional; left empty it is filled with a generated random key at save time
# (InviteService.config_form_section post_merge). A self-chosen key is kept as-is.
INVITE_KEY_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "key"),
    validator=InviteKeyValidator(),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)

INVITE_REALM_ROLE_ITEM_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "realm-roles[*]"),
    values_provider="InviteRealmRoleOptionsProvider",
    validator=RealmRoleValidator(),
    virtualize=_VIRTUALIZE,
)

INVITE_REALM_ROLES_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "realm-roles"),
    min_items=0,
    max_items=10,
    children=[INVITE_REALM_ROLE_ITEM_EDITABLE],
    virtualize=_VIRTUALIZE,
)

# Kept, but no longer offered in the form (see visualizers): it restricts by email DOMAIN
# ("@rijksoverheid.nl"), which reads like a list of allowed addresses and is not that.
# Existing files keep validating and the restriction keeps being enforced in
# InviteManager.validate_email_domain; only the UI stopped offering it.
INVITE_RESTRICT_DOMAIN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "restrict-domain"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)

INVITE_CONTACT_EMAIL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "contact-email"),
    validator=EmailValidator(),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_contact_email,
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)

# Not required, unlike the texts: there is no default we can compute for it. Deriving it
# would mean picking a component and a deployment and rebuilding the hostname from the
# domain format, and a success button pointing at the wrong address is worse than a success
# page without a button, which is what an empty value yields.
INVITE_APPLICATION_URL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "application-url"),
    validator=UrlValidator(),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)

# Closed set (sso/local); the model types it as list[Literal[...]] so it is guardrailed
# regardless of the widget. Empty means: fall back to the project auth methods.
#
# An invite can only NARROW what the realm offers, never widen it:
#     allow_sso = realm_auth["sso"] and invite_auth_config["sso"]   (invite_routes)
# The realm follows the keycloak template, and the two blueprints differ exactly here:
# sso-only sets registrationAllowed/loginWithEmailAllowed to false (SSO is the only way in),
# sso-support sets both to true. So under sso-only there is one possible answer and the
# field is inert -- it is hidden rather than shown as a choice that changes nothing.
INVITE_AUTH_METHODS_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "auth-methods"),
    values_provider="InviteAuthMethodOptionsProvider",
    depends_on="services/keycloak/config/template",
    show_when={"value": ["sso-support"]},
    virtualize=_VIRTUALIZE,
)

# --- i18n texts (two editables per text: nl + en) ----------------------------

# All six are required and all six carry a computed default, so a fresh invite starts out
# filled in rather than blank. Required without a default would just be an obstacle; a
# default without required would let someone empty the field and end up with a page that
# shows nothing.
INVITE_MESSAGE_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "message", "nl"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_message_nl,
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_MESSAGE_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "message", "en"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_message_en,
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_TITLE_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-title", "nl"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_title_nl,
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_TITLE_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-title", "en"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_title_en,
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_BUTTON_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-button", "nl"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_button_nl,
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_BUTTON_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-button", "en"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_button_en,
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)

# The children of one ``active`` item, in display order.
INVITE_ITEM_CHILD_EDITABLES = [
    INVITE_KEY_EDITABLE,
    INVITE_REALM_ROLES_EDITABLE,
    INVITE_RESTRICT_DOMAIN_EDITABLE,
    INVITE_CONTACT_EMAIL_EDITABLE,
    INVITE_APPLICATION_URL_EDITABLE,
    INVITE_AUTH_METHODS_EDITABLE,
    INVITE_MESSAGE_NL_EDITABLE,
    INVITE_MESSAGE_EN_EDITABLE,
    INVITE_SUCCESS_TITLE_NL_EDITABLE,
    INVITE_SUCCESS_TITLE_EN_EDITABLE,
    INVITE_SUCCESS_BUTTON_NL_EDITABLE,
    INVITE_SUCCESS_BUTTON_EN_EDITABLE,
]

# The schema models ``active`` as a list because Keycloak-side nothing stops a project from
# handing out several links with different roles. In practice every project has exactly one,
# and a sequence widget for a single item is all overhead: an add button that should not be
# used, a remove button that empties the service, and a numbered card around one form.
#
# So it stays a list on disk (no migration, existing multi-entry files keep working and keep
# being served) while the form pins it to exactly one: min == max == 1 and no add/remove.
# Raise these two numbers to hand the list back to the user; nothing else has to change.
INVITE_ACTIVE_EDITABLE = Editable(
    yaml_path=_cp("active"),
    min_items=1,
    max_items=1,
    add_remove=False,
    children=INVITE_ITEM_CHILD_EDITABLES,
    virtualize=_VIRTUALIZE,
)

# Flat list of every editable this service contributes at the project layer.
INVITE_EDITABLES = [
    INVITE_DEFAULT_LANGUAGE_EDITABLE,
    INVITE_ACTIVE_EDITABLE,
    *INVITE_ITEM_CHILD_EDITABLES,
    INVITE_REALM_ROLE_ITEM_EDITABLE,
]
