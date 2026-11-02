"""De leden van een project lezen, met het verschil tussen een mens en een service account.

``users`` in een projectbestand draagt sinds schemaversie 2.12 twee soorten lid. Alles wat
met een MENS gebeurt (een Keycloak-gebruiker, een uitnodiging, een mail, OTP, de
inloglijst) hoort alleen de mensen te zien, en alles wat met een BEWIJSMIDDEL gebeurt ziet
ze allebei. Dat onderscheid staat hier op een plek, zodat een nieuwe lezer het niet opnieuw
hoeft te bedenken en ``tests/test_human_members_guard.py`` kan zien wie het overslaat.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from opi.core.leden import LidSoort

if TYPE_CHECKING:
    from opi.services.project_service import ProjectSummary, ProjectUser

#: Waar een uitgegeven token mee begint. De oude projectsleutel heeft geen voorvoegsel; aan
#: dit voorvoegsel ziet een lezer dus dat hij naar een token kijkt en niet naar de sleutel
#: die de migratie van 2.12 heeft overgenomen.
TOKEN_PREFIX = "zad_"  # noqa: S105 -- voorvoegsel van een token, geen secret

#: Aantal willekeurige bytes in een token. 32 bytes (256 bits) is te veel om te raden, dus
#: de hash eronder hoeft geen wachtwoordhash te zijn; sha256 kost niets op het hete pad.
TOKEN_BYTES = 32


def nieuw_token() -> tuple[str, str]:
    """Een nieuw token en zijn hash. De waarde komt hier een keer vandaan en nooit meer."""
    waarde = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    return waarde, hash_van(waarde)


def hash_van(waarde: str) -> str:
    """De opslagvorm van een token: sha256 in hex, nooit de waarde zelf."""
    return hashlib.sha256(waarde.encode()).hexdigest()


def vervaldatum(dagen: int) -> datetime:
    """De vervaldatum van een token dat over ``dagen`` dagen verloopt."""
    return datetime.now(UTC) + timedelta(days=dagen)


def is_verlopen(vervalt: datetime, *, nu: datetime | None = None) -> bool:
    """Of deze vervaldatum voorbij is. Een datum zonder tijdzone geldt als UTC."""
    if vervalt.tzinfo is None:
        vervalt = vervalt.replace(tzinfo=UTC)
    return vervalt <= (nu or datetime.now(UTC))


def human_members(project: ProjectSummary) -> list[ProjectUser]:
    """De MENSEN in dit project, zonder de service accounts.

    Elke lezer die een Keycloak-gebruiker aanmaakt, een uitnodiging of een mail stuurt, OTP
    aanbiedt of de inloglijst vult gebruikt deze functie. Een service account heeft geen
    postvak en geen tweede factor, dus zou daar een gebruiker opleveren die niemand kan
    gebruiken en wel toegang geeft.
    """
    return [lid for lid in project.users or [] if lid.type is not LidSoort.SERVICE_ACCOUNT]


def human_emails(project: ProjectSummary) -> list[str]:
    """De e-mailadressen van de mensen in dit project."""
    return [lid.email for lid in human_members(project) if lid.email]


def service_accounts(project: ProjectSummary) -> list[ProjectUser]:
    """De service accounts in dit project."""
    return [lid for lid in project.users or [] if lid.type is LidSoort.SERVICE_ACCOUNT]


def lid_met_naam(project: ProjectSummary, naam: str) -> ProjectUser | None:
    """Het lid dat zo heet: een e-mailadres of de naam van een service account."""
    for lid in project.users or []:
        if lid.principaal.lower() == naam.lower():
            return lid
    return None


def menselijke_leden_uit_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Dezelfde filter op RUWE projectdata, voor lezers die geen ProjectSummary hebben."""
    leden = data.get("users")
    if not isinstance(leden, list):
        return []
    return [
        lid
        for lid in leden
        if isinstance(lid, dict) and lid.get("type", LidSoort.PERSOON.value) != LidSoort.SERVICE_ACCOUNT.value
    ]
