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
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)

INVITE_APPLICATION_URL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "application-url"),
    validator=UrlValidator(),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)

# Closed set (sso/local); the model types it as list[Literal[...]] so it is guardrailed
# regardless of the widget. Empty means: fall back to the project auth methods.
INVITE_AUTH_METHODS_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "auth-methods"),
    values_provider="InviteAuthMethodOptionsProvider",
    virtualize=_VIRTUALIZE,
)

# --- i18n texts (two editables per text: nl + en) ----------------------------

INVITE_MESSAGE_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "message", "nl"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_MESSAGE_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "message", "en"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_TITLE_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-title", "nl"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_TITLE_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-title", "en"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_BUTTON_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-button", "nl"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_VIRTUALIZE,
)
INVITE_SUCCESS_BUTTON_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-button", "en"),
    converter=EmptyToNoneConverter(),
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

INVITE_ACTIVE_EDITABLE = Editable(
    yaml_path=_cp("active"),
    min_items=0,
    max_items=20,
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
