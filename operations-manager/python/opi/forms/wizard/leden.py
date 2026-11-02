"""Wat de teamvorm van de ledenlijst bezit, en wat hij ervan terugzet.

``users`` draagt sinds schemaversie 2.12 twee soorten lid en de tokenhashes van beide,
terwijl het teamformulier alleen het e-mailadres en de rol van een MENS rendert. Een
formulier dat die lijst als geheel schrijft gooit daarmee elk token en elk service account
weg, op een handeling die "Projectleden bijgewerkt" heet. Dit is dezelfde scheiding die
:mod:`opi.forms.wizard.secrets` voor versleutelde waarden maakt: houd uit het formulier
wat het niet bewerkt, en zet het bij het opslaan terug.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from opi.core.leden import LidSoort, ledenlijst_wijzigt
from opi.services.api_token_service import TokenAanvraagGeweigerd, eis_een_admin, schoon_rollen

logger = logging.getLogger(__name__)

#: Velden van een lid die het formulier niet rendert en dus ook niet schrijft.
VERBORGEN_LEDENVELDEN = ("tokens",)


class LedenlijstGeweigerd(ValueError):
    """De ingediende ledenlijst mag zo niet opgeslagen worden."""


def _is_service_account(lid: dict[str, Any]) -> bool:
    return lid.get("type") == LidSoort.SERVICE_ACCOUNT.value


def leden_voor_formulier(data: dict[str, Any]) -> dict[str, Any]:
    """*data* met in ``users`` alleen de mensen, zonder hun verborgen velden.

    Een service-accountregel in het formulier landt als lid zonder adres, en geen uitkomst
    laat hem overleven: leegmaken valt op de verplichte-veldcontrole, weghalen verwijdert
    het account en een adres invullen laat het schema de regel weigeren.
    """
    leden = data.get("users")
    if not isinstance(leden, list):
        return data
    mensen = [
        {k: v for k, v in lid.items() if k not in VERBORGEN_LEDENVELDEN}
        for lid in leden
        if isinstance(lid, dict) and not _is_service_account(lid)
    ]
    return {**data, "users": mensen}


def zet_verborgen_leden_terug(merged_data: dict[str, Any], stored_data: dict[str, Any]) -> None:
    """Zet in ``merged_data`` terug wat het formulier van ``users`` niet draagt.

    De service accounts komen terug zoals ze in het bestand staan, en de tokens van een
    mens komen terug op zijn e-mailadres. Wie uit de lijst is gehaald, is zijn tokens kwijt:
    dat is wat het verwijderen van een lid hoort te doen.
    """
    ingediend = merged_data.get("users")
    stored = stored_data.get("users")
    if not isinstance(ingediend, list) or not isinstance(stored, list):
        return

    service_accounts = [lid for lid in stored if isinstance(lid, dict) and _is_service_account(lid)]
    verborgen = {
        str(lid["email"]).lower(): lid
        for lid in stored
        if isinstance(lid, dict) and not _is_service_account(lid) and lid.get("email")
    }

    mensen: list[Any] = []
    for lid in ingediend:
        if not isinstance(lid, dict):
            mensen.append(lid)
            continue
        if _is_service_account(lid):
            # Het formulier maakt geen service account, dus zo'n regel is er niet uit hem.
            continue
        nieuw = {k: v for k, v in lid.items() if k not in VERBORGEN_LEDENVELDEN}
        opgeslagen = verborgen.get(str(lid.get("email") or "").lower())
        if opgeslagen:
            nieuw.update(
                {veld: copy.deepcopy(opgeslagen[veld]) for veld in VERBORGEN_LEDENVELDEN if veld in opgeslagen}
            )
        mensen.append(nieuw)

    gebleven = {str(lid.get("email") or "").lower() for lid in mensen if isinstance(lid, dict)}
    vertrokken = [adres for adres, lid in verborgen.items() if adres not in gebleven and lid.get("tokens")]
    if vertrokken:
        logger.info("Tokens ingetrokken met het lid dat ze bezat: %s", ", ".join(sorted(vertrokken)))

    merged_data["users"] = mensen + copy.deepcopy(service_accounts)


def bewaak_ledenlijst(merged_data: dict[str, Any], stored_data: dict[str, Any], *, bezit_users: bool) -> None:
    """Weiger een inzending die ``users`` schrijft zonder die lijst te bezitten.

    Een flow die het teamformulier niet rendert, rendert ook geen rollen; een inzending
    die ``users`` toch anders meebrengt komt dus niet uit een formulier. Weigeren in
    plaats van negeren, zodat een gemanipuleerde POST zichtbaar afketst (ontwerp
    paragraaf 7).

    De drie regels OVER de rollen zelf komen uit :mod:`opi.services.api_token_service`:
    de wizard schrijft de hele lijst in een keer en de rolroute een lid per keer, maar
    wat een geldige rol is en dat er een admin overblijft, staat op een plek.

    Raises:
        LedenlijstGeweigerd: Bij een wijziging van ``users`` door een flow die hem niet
            bezit, bij een onbekende rol, bij een lid zonder rollen, of als het project
            zonder admin zou achterblijven.
    """
    ingediend = merged_data.get("users")
    if not bezit_users:
        if ledenlijst_wijzigt(stored_data, merged_data):
            raise LedenlijstGeweigerd("Deze stap bewerkt de projectleden niet.")
        return

    if not isinstance(ingediend, list) or not ingediend:
        raise LedenlijstGeweigerd("Een project houdt minstens een lid.")

    for lid in ingediend:
        if not isinstance(lid, dict):
            continue
        try:
            schoon_rollen(lid.get("roles") or [])
        except TokenAanvraagGeweigerd as exc:
            raise LedenlijstGeweigerd(f"{exc} ({lid.get('email') or lid.get('name')})") from exc

    try:
        eis_een_admin(merged_data)
    except TokenAanvraagGeweigerd as exc:
        raise LedenlijstGeweigerd(str(exc)) from exc
