"""Of het realm van dit project een SSO-identiteit op e-mailadres koppelt.

Eén lezer voor die vraag, omdat er drie zijn: de uitnodigingsweg (``get_invite_auth_methods``),
de weigering bij opslaan (``InviteService.validate_project``) en de keuzelijst in het formulier.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.keycloak.config_model import AccountLink
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

#: De standen waarvoor ``KeycloakYamlHandler`` de first-broker-login-flow met ``idp-auto-link``
#: installeert. Elke andere stand, ``verify`` en niets gekozen inbegrepen, laat Keycloaks eigen
#: flow staan, en die laat de gebruiker het bestaande account eerst bewijzen.
LINKING_MODES: tuple[AccountLink, ...] = (AccountLink.AUTOMATIC, AccountLink.CONFIRM)


def account_link_mode(keycloak_config: Any) -> str | None:
    """De koppelstand in één keycloak-config, of None als die niet op e-mail koppelt.

    Losse ingang naast ``realm_account_link_mode``, want het formulier heeft de config al
    (de wizard houdt hem onder een andere root dan het projectbestand) en zou hem hier
    anders een tweede keer moeten opzoeken.

    Beide schrijfwijzen van de sleutel worden gelezen: ``KeycloakConfig`` valideert ze dankzij
    ``populate_by_name`` allebei, dus alleen de koppeltekenvorm lezen maakt de andere een
    stille no-op.
    """
    if not isinstance(keycloak_config, dict):
        return None
    mode = next((keycloak_config[key] for key in ("account-link", "account_link") if key in keycloak_config), None)
    return str(mode) if mode in LINKING_MODES else None


def realm_account_link_mode(project_data: dict[str, Any]) -> str | None:
    """De koppelstand van het projectrealm, of None als het realm niet op e-mail koppelt."""
    for entry in project_data.get("services") or []:
        if service_entry_name(entry) == ServiceType.KEYCLOAK.value:
            return account_link_mode(service_entry_config(entry))
    return None


def realm_links_on_email(project_data: dict[str, Any]) -> bool:
    """True als een brokered SSO-identiteit in dit realm op e-mailadres gekoppeld wordt.

    Het koppeldoel is dan elk account in het realm met hetzelfde adres, dus een account dat
    met een zelf ingetypt adres is aangemaakt neemt een identiteit over die iemand anders
    toekomt. Vandaar dat de lokale uitnodigingsweg hier dicht is.
    """
    return realm_account_link_mode(project_data) is not None
