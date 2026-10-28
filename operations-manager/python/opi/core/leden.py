"""Wat voor soort lid er in ``users`` van een projectbestand staat."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class LidSoort(StrEnum):
    """Een mens, of een service account.

    Een service account is geen aparte lijst naast ``users`` maar een lid in die lijst: hij
    draagt dezelfde rollen en kan dezelfde tokens hebben, maar er zit geen mens achter. Dat
    verschil telt bij elke lezer die iets met een MENS doet (Keycloak, uitnodiging, mail,
    OTP, de inloglijst); die lezen daarom
    :func:`opi.services.project_leden.human_members` en niet ``users`` zelf.
    """

    PERSOON = "person"
    SERVICE_ACCOUNT = "service-account"


def ledenlijst_wijzigt(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Of deze opslag de ledenlijst of de rollen van een lid raakt.

    Wie deze vraag stelt, stelt hem over een poort of een step-up, dus staat hij hier
    een keer: de drie wegen die leden kunnen schrijven moeten dezelfde grens zien. Tot
    RC-392 kon de team-wizard een lid toevoegen en elke rol zetten zonder step-up, en kon
    de bewerkwizard dat zonder de rolpoort, terwijl de Team-kaart beide wel vroeg.
    """
    return _leden_met_rollen(before) != _leden_met_rollen(after)


def _leden_met_rollen(data: dict[str, Any]) -> set[tuple[str, tuple[str, ...]]]:
    """De leden met hun rollen, in een vorm die alleen op die twee vergelijkt."""
    return {
        (
            str(lid.get("email") or lid.get("name") or ""),
            tuple(sorted(str(rol) for rol in (lid.get("roles") or []))),
        )
        for lid in data.get("users") or []
        if isinstance(lid, dict)
    }
