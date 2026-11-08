"""Whether this project's realm links an SSO identity on email address.

One reader for that question, because there are three askers: the invite path
(``get_invite_auth_methods``), the refusal on save (``InviteService.validate_project``) and the
option list in the form.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.keycloak.config_model import AccountLink
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

#: The settings for which ``KeycloakYamlHandler`` installs the first-broker-login flow with
#: ``idp-auto-link``. Every other setting, ``verify`` and nothing chosen included, leaves
#: Keycloak's own flow in place, which makes the user prove the existing account first.
LINKING_MODES: tuple[AccountLink, ...] = (AccountLink.AUTOMATIC, AccountLink.CONFIRM)


def account_link_mode(keycloak_config: Any) -> str | None:
    """The link setting in one keycloak config, or None when it does not link on email.

    A separate entry point next to ``realm_account_link_mode``, because the form already has
    the config (the wizard keeps it under a different root than the project file) and would
    otherwise have to look it up a second time here.

    Both spellings of the key are read: thanks to ``populate_by_name`` ``KeycloakConfig``
    validates both, so reading only the hyphenated shape would make the other a silent no-op.
    """
    if not isinstance(keycloak_config, dict):
        return None
    mode = next((keycloak_config[key] for key in ("account-link", "account_link") if key in keycloak_config), None)
    return str(mode) if mode in LINKING_MODES else None


def realm_account_link_mode(project_data: dict[str, Any]) -> str | None:
    """The link setting of the project realm, or None when the realm does not link on email."""
    for entry in project_data.get("services") or []:
        if service_entry_name(entry) == ServiceType.KEYCLOAK.value:
            return account_link_mode(service_entry_config(entry))
    return None


def realm_links_on_email(project_data: dict[str, Any]) -> bool:
    """True when a brokered SSO identity in this realm is linked on email address.

    The link target is then every account in the realm with the same address, so an account
    created with a hand-typed address takes over an identity that belongs to someone else.
    Hence the local invite path is closed here.
    """
    return realm_account_link_mode(project_data) is not None
