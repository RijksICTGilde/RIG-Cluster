"""De realmblauwdrukken waaruit een project kiest, en wat ze over lokale accounts zeggen.

Eén plek, want de regel "deze blauwdruk laat een lokaal account inloggen" werd op twee
manieren gesteld: ``template != "sso-only"`` in ``InviteAuthMethodOptionsProvider`` en een
opsomming ``["sso-support"]`` in de ``show_when`` van de uitnodigingsvelden. Met een derde
blauwdruk erbij spreken die twee elkaar tegen: de opsomming laat de nieuwe eruit en de
toets laat hem erin.
"""

from __future__ import annotations

#: SSO redirect only; Keycloak shows no login screen. Default template when omitted.
SSO_ONLY_TEMPLATE = "sso-only"

#: Het inlogscherm van Keycloak laat de keuze: SSO Rijk of een lokaal account.
SSO_SUPPORT_TEMPLATE = "sso-support"

#: Zelfde keuze, maar een lokaal account geeft naast zijn wachtwoord een OTP.
SSO_SUPPORT_OTP_TEMPLATE = "sso-support-otp"

#: De blauwdrukken waarin een lokaal account een weg naar binnen is.
LOCAL_ACCOUNT_TEMPLATES = (SSO_SUPPORT_TEMPLATE, SSO_SUPPORT_OTP_TEMPLATE)


def allows_local_accounts(template: str | None) -> bool:
    """Of deze blauwdruk een inlogscherm toont waarop een lokaal account past.

    Een leeg template betekent ``sso-only``: dat is de standaard van
    ``KeycloakConfig.template`` en dus wat de realm werkelijk krijgt.
    """
    return (template or SSO_ONLY_TEMPLATE) in LOCAL_ACCOUNT_TEMPLATES
