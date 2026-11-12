"""Editable definitions for the invite service (project-level config).

All fields live under ``services/invite/config``. Paths are built with ``config_path``
so the layer/service is encoded once (enum-driven) instead of a hardcoded string. Every
editable carries ``virtualize=SERVICE_VIRTUALIZE`` so project-level service config does
not collide with the service-selection list in the wizard state.

The ``active`` sequence holds one item per invitation; its children are the invite fields.
The realm-roles picker is a sequence of selects fed by ``InviteRealmRoleOptionsProvider``
(reads the keycloak config from the surrounding form data). ``groups`` and ``client_roles``
are deliberately NOT offered here (advanced pass-through, see the config model).
"""

from __future__ import annotations

from opi.forms.editables.converters import EmptyToNoneConverter, InviteTargetConverter, NonEmptyListConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
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
from opi.services.catalog.keycloak.templates import LOCAL_ACCOUNT_TEMPLATES
from opi.services.services_enums import ServiceType


def _cp(*segments: str) -> str:
    return config_path(ConfigLayer.PROJECT, ServiceType.INVITE, "config", *segments)


INVITE_DEFAULT_LANGUAGE_EDITABLE = Editable(
    yaml_path=_cp("default-language"),
    values_provider="InviteLanguageOptionsProvider",
    validator=AllowedValuesValidator(["nl", "en"]),
    default="nl",
    virtualize=SERVICE_VIRTUALIZE,
)

# Optional; left empty it is filled with a generated random key at save time
# (InviteService.config_form_section post_merge). A self-chosen key is kept as-is.
INVITE_KEY_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "key"),
    validator=InviteKeyValidator(),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

# ``values_must_exist``: the role has to EXIST as well. ``RealmRoleValidator`` only judges the
# character set, so a typo like 'allowd-user' passes it unhindered; keycloak does not know that
# role, redemption skips it (``assign_realm_roles_to_user`` puts it under ``not_found``) and the
# invited user arrives without a role -- with ``restrict-access`` on, not at all. The valid set
# is not repeated here: it comes from the same provider that fills the option list and that the
# API publishes as ``x-choices-source``.
INVITE_REALM_ROLE_ITEM_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "realm-roles[*]"),
    values_provider="InviteRealmRoleOptionsProvider",
    values_must_exist=True,
    validator=RealmRoleValidator(),
    virtualize=SERVICE_VIRTUALIZE,
)

#: The schema allows several roles and storage stays a list, but in practice there is always
#: one. Hence min_items == max_items == 1: the sequence template then leaves out the add and
#: remove buttons and shows exactly one option list, always visible. Whoever wants to assign no
#: role picks the empty option; that writes no role.
INVITE_REALM_ROLES_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "realm-roles"),
    min_items=1,
    max_items=1,
    children=[INVITE_REALM_ROLE_ITEM_EDITABLE],
    converter=NonEmptyListConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

# Accepted, but not offered in the form (see visualizers): it restricts by email DOMAIN
# ("@rijksoverheid.nl"), which reads like a list of allowed addresses and is not that.
# Existing files keep validating and the restriction keeps being enforced in
# InviteManager.validate_email_domain.
INVITE_RESTRICT_DOMAIN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "restrict-domain"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

INVITE_CONTACT_EMAIL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "contact-email"),
    validator=EmailValidator(),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_contact_email,
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

# Picked from the project's own public addresses rather than typed. Someone setting up an
# invitation knows which deployment and component people should land on; the hostname is
# derived from the domain format, the subdomain and the cluster, so asking for a URL asks
# for something they would have to look up and could get wrong.
#
# And for the same reason the ANSWER is not what gets stored: all three of those inputs can
# change, and then a stored address points at a hostname that does not exist while
# everything needed to work out the new one is still in the project file. So the field
# writes ``application-target`` -- ``component:deployment[:/pad]``, the choice -- and the
# address is derived again every time the success page is rendered.
#
# ``application-url`` is still accepted, still valid and NOT deprecated: it is the shape for
# a destination OUTSIDE this project, and it is what every existing invitation still stores,
# because those are not rewritten. This picker cannot offer such an address, so it does not
# own that field either; a save through the portal leaves it alone unless a target is chosen
# (``InviteService.settle_destination``).
#
# Still not required: a project without publish-on-web has no address to offer, and an
# invitation without a destination simply shows no button.
INVITE_APPLICATION_TARGET_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "application-target"),
    values_provider="InviteApplicationUrlOptionsProvider",
    validator=UrlValidator(),
    converter=InviteTargetConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

# Closed set (sso/local); the model types it as list[Literal[...]] so it is guardrailed
# regardless of the widget. Empty means: fall back to the project auth methods.
#
# An invite can only NARROW what the realm offers, never widen it: invite_routes ANDs the
# realm's flag per method with the invite's. The realm follows the keycloak template, and the blueprints differ exactly here:
# sso-only shows no login form (SSO is the only way in); sso-support and sso-support-otp
# show Keycloak's login form. Under sso-only there is one possible answer and the field
# is inert: it is hidden rather than shown as a choice that changes nothing.
INVITE_AUTH_METHODS_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "auth-methods"),
    values_provider="InviteAuthMethodOptionsProvider",
    depends_on="services/keycloak/config/template",
    show_when={"value": list(LOCAL_ACCOUNT_TEMPLATES)},
    virtualize=SERVICE_VIRTUALIZE,
)

# The i18n texts: two editables per text, nl and en. All six are required and all six carry a
# computed default, so a fresh invite starts out
# filled in rather than blank. Required without a default would just be an obstacle; a
# default without required would let someone empty the field and end up with a page that
# shows nothing.
# The title is the exception: optional and without a default, because its fallback is the
# project's display name at render time. A computed default would freeze today's display
# name into the file.
INVITE_TITLE_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "title", "nl"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
INVITE_TITLE_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "title", "en"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
INVITE_MESSAGE_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "message", "nl"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_message_nl,
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
INVITE_MESSAGE_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "message", "en"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_message_en,
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
INVITE_SUCCESS_TITLE_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-title", "nl"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_title_nl,
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
INVITE_SUCCESS_TITLE_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-title", "en"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_title_en,
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
INVITE_SUCCESS_BUTTON_NL_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-button", "nl"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_button_nl,
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
INVITE_SUCCESS_BUTTON_EN_EDITABLE = Editable(
    yaml_path=_cp("active[*]", "success-button", "en"),
    converter=EmptyToNoneConverter(),
    required=True,
    default=default_success_button_en,
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

# The children of one ``active`` item, in display order.
INVITE_ITEM_CHILD_EDITABLES = [
    INVITE_KEY_EDITABLE,
    INVITE_REALM_ROLES_EDITABLE,
    INVITE_RESTRICT_DOMAIN_EDITABLE,
    INVITE_CONTACT_EMAIL_EDITABLE,
    INVITE_APPLICATION_TARGET_EDITABLE,
    INVITE_AUTH_METHODS_EDITABLE,
    INVITE_TITLE_NL_EDITABLE,
    INVITE_TITLE_EN_EDITABLE,
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
#
# The API does the same, with a switch of its own: ``InviteService`` sets
# ``api_singular_lists = {"active"}``, which makes the PUT and the GET show one invitation
# instead of a list of one. Two layers, two switches, the same reason: they are deliberately
# not tied together (the form is about a widget, the facade about a body), so whoever flips one
# takes a look at the other.
INVITE_ACTIVE_EDITABLE = Editable(
    yaml_path=_cp("active"),
    min_items=1,
    max_items=1,
    add_remove=False,
    children=INVITE_ITEM_CHILD_EDITABLES,
    virtualize=SERVICE_VIRTUALIZE,
)

# Flat list of every editable this service contributes at the project layer.
INVITE_EDITABLES = [
    INVITE_DEFAULT_LANGUAGE_EDITABLE,
    INVITE_ACTIVE_EDITABLE,
    *INVITE_ITEM_CHILD_EDITABLES,
    INVITE_REALM_ROLE_ITEM_EDITABLE,
]
