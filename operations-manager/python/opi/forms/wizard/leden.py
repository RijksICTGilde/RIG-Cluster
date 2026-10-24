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

from opi.core.leden import LidSoort

logger = logging.getLogger(__name__)

#: Velden van een lid die het formulier niet rendert en dus ook niet schrijft.
VERBORGEN_LEDENVELDEN = ("tokens",)


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
