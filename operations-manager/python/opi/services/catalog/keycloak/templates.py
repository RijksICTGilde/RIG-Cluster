"""The realm blueprints a project picks from, and what they say about local accounts.

One place for the rule "this blueprint lets a local account sign in": a second statement of
it (a listing of blueprint names in a ``show_when``, say) disagrees with this one as soon as
a blueprint is added -- the listing leaves the new one out while the check lets it in.
"""

from __future__ import annotations

#: SSO redirect only; Keycloak shows no login screen. Default template when omitted.
SSO_ONLY_TEMPLATE = "sso-only"

#: Keycloak's login screen offers the choice: SSO Rijk or a local account.
SSO_SUPPORT_TEMPLATE = "sso-support"

#: The same choice, but a local account supplies an OTP alongside its password.
SSO_SUPPORT_OTP_TEMPLATE = "sso-support-otp"

#: The blueprints in which a local account is a way in.
LOCAL_ACCOUNT_TEMPLATES = (SSO_SUPPORT_TEMPLATE, SSO_SUPPORT_OTP_TEMPLATE)


def allows_local_accounts(template: str | None) -> bool:
    """Whether this blueprint shows a login screen a local account fits on.

    An empty template means ``sso-only``: that is the default of ``KeycloakConfig.template``
    and therefore what the realm really gets.
    """
    return (template or SSO_ONLY_TEMPLATE) in LOCAL_ACCOUNT_TEMPLATES
