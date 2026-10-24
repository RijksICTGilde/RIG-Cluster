"""Leden en hun tokens beheren: wie mag wat, en wat is een geldige aanvraag.

Een laag boven :class:`opi.services.project_store.ProjectStore`, zodat de API en het
formulier hetzelfde logicapad lopen: de vervaldatum, de rol van de aanvrager en de vorm van
een naam staan hier en niet in een route.

Lidmaatschap in het projectbestand is de enige bron van wat iemand hier mag. Ook een
platformbeheerder die geen lid van dit project is beheert er geen leden en geen tokens:
een token is nooit meer dan het lid waar het bij hoort, en wie geen lid is heeft er geen.

Alles wat hier gebeurt is een COMMIT op het projectbestand. Dat is de prijs voor tokens die
naast hun lid staan in plaats van in een eigen tabel, en het is ook de winst: uitgeven,
intrekken en een rol veranderen staan daarmee in dezelfde reviewbare historie als de rest
van de samenstelling van een project.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from opi.api.principaal import SCHRIJFROLLEN
from opi.core.actor import user_actor
from opi.core.config import settings
from opi.core.leden import LidSoort
from opi.services.project_leden import TOKEN_PREFIX, hash_van, is_verlopen, lid_met_naam, nieuw_token, vervaldatum
from opi.services.project_store import get_project_store

if TYPE_CHECKING:
    from datetime import datetime

    from opi.services.project_service import ProjectUser

logger = logging.getLogger(__name__)

#: De rollen die een lid kan hebben. Dezelfde vier als in het schema.
ROLLEN = ("admin", "owner", "member", "developer")

#: Een service-accountnaam is een DNS-label: hij reist mee in de actor en in de
#: git-trailer, dus hij moet overal als een label te schrijven zijn.
NAAM_PATROON = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")

_TOKENNAAM_PATROON = re.compile(r"^[\w .-]{1,64}$")


class TokenAanvraagGeweigerd(Exception):
    """De aanvraag klopt niet, of de aanvrager mag hem niet doen. Boodschap is voor de gebruiker."""


def _vervaldatum(dagen: int, maximum: int) -> datetime:
    """De vervaldatum van een nieuw token, of een weigering.

    Er is geen tak zonder vervaldatum: een uitzondering die eenmaal bestaat wordt de norm.
    """
    if dagen < 1:
        raise TokenAanvraagGeweigerd("Een token moet minstens een dag geldig zijn.")
    if dagen > maximum:
        raise TokenAanvraagGeweigerd(f"Een token mag hoogstens {maximum} dagen geldig zijn.")
    return vervaldatum(dagen)


def _lid(project_name: str, naam: str) -> ProjectUser | None:
    """Het lid dat zo heet in dit project, of None als het er niet staat."""
    project = get_project_store().get(project_name)
    return lid_met_naam(project, naam) if project is not None else None


def _rol(project_name: str, naam: str) -> str | None:
    """De huidige rol van dit lid in dit project, of None als het geen lid is."""
    lid = _lid(project_name, naam)
    return lid.role if lid is not None else None


def _eis_lid(project_name: str, door: str) -> None:
    """Weiger wie geen lid is van dit project, ook een platformbeheerder.

    Zonder deze eis gaf een intrekking op een BESTAAND project een stille no-op en op een
    onbestaand een 500, en dat verschil leest als "bestaat deze projectnaam".
    """
    if _rol(project_name, door) is None:
        raise TokenAanvraagGeweigerd("Je bent geen lid van dit project.")


def is_projectbeheerder(project_name: str, naam: str) -> bool:
    """Of dit lid de leden en tokens van dit project mag beheren.

    Voor de interface, zodat een knop dezelfde bron leest als de weigering erachter: een
    platformbeheerder die geen lid is krijgt hier False en ziet de knoppen dus niet.
    """
    return _rol(project_name, naam) in SCHRIJFROLLEN


def _eis_beheerder(project_name: str, door: str) -> None:
    if _rol(project_name, door) not in SCHRIJFROLLEN:
        raise TokenAanvraagGeweigerd("Alleen een beheerder van dit project kan dit doen.")


def _leden(data: dict[str, Any]) -> list[dict[str, Any]]:
    leden = data.get("users")
    if not isinstance(leden, list):
        raise TokenAanvraagGeweigerd("Dit projectbestand heeft geen ledenlijst.")
    return leden


def _zoek_lid(data: dict[str, Any], naam: str) -> dict[str, Any] | None:
    for lid in _leden(data):
        if not isinstance(lid, dict):
            continue
        eigen = lid.get("name") if lid.get("type") == LidSoort.SERVICE_ACCOUNT.value else lid.get("email")
        if str(eigen or "").lower() == naam.lower():
            return lid
    return None


def _eis_tokennaam(naam: str) -> str:
    schoon = naam.strip()
    if not _TOKENNAAM_PATROON.match(schoon):
        raise TokenAanvraagGeweigerd("Geef het token een korte naam van letters, cijfers, spatie, punt of streepje.")
    return schoon


async def _wijzig(project_name: str, change: Any, *, bericht: str, door: str) -> None:
    await get_project_store().mutate(project_name, change, message=bericht, actor=user_actor(door))


async def voeg_service_account_toe(*, project_name: str, naam: str, rol: str, door: str) -> None:
    """Zet een service account in de ledenlijst van dit project."""
    _eis_beheerder(project_name, door)
    if not NAAM_PATROON.match(naam):
        raise TokenAanvraagGeweigerd(
            "Een naam bestaat uit kleine letters, cijfers en streepjes, en begint met een letter."
        )
    if rol not in ROLLEN:
        raise TokenAanvraagGeweigerd(f"Onbekende rol '{rol}'.")

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        if _zoek_lid(data, naam) is not None:
            return None
        _leden(data).append({"type": LidSoort.SERVICE_ACCOUNT.value, "name": naam, "role": rol, "tokens": []})
        return data

    await _wijzig(project_name, change, bericht=f"Service account '{naam}' toegevoegd", door=door)


async def verwijder_service_account(*, project_name: str, naam: str, door: str) -> None:
    """Haal een service account uit de ledenlijst. Zijn tokens gaan er mee uit."""
    _eis_beheerder(project_name, door)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        lid = _zoek_lid(data, naam)
        if lid is None or lid.get("type") != LidSoort.SERVICE_ACCOUNT.value:
            return None
        _leden(data).remove(lid)
        return data

    await _wijzig(project_name, change, bericht=f"Service account '{naam}' verwijderd", door=door)


async def wijzig_rol(*, project_name: str, naam: str, rol: str, door: str) -> None:
    """Geef een lid een andere rol. Zijn tokens volgen die rol meteen."""
    _eis_beheerder(project_name, door)
    if rol not in ROLLEN:
        raise TokenAanvraagGeweigerd(f"Onbekende rol '{rol}'.")

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        lid = _zoek_lid(data, naam)
        if lid is None:
            raise TokenAanvraagGeweigerd(f"'{naam}' is geen lid van dit project.")
        if lid.get("role") == rol:
            return None
        lid["role"] = rol
        return data

    await _wijzig(project_name, change, bericht=f"Rol van '{naam}' is nu '{rol}'", door=door)


def _eis_mag_uitgeven(project_name: str, naam: str, door: str) -> int:
    """Weiger wie dit lid geen token mag geven, en geef het platformmaximum terug.

    Een beheerder doet dat voor een service account van zijn project; een mens doet het
    voor zichzelf. Een mens kan dus geen token voor een ander maken: dat zou een
    bewijsmiddel zijn dat iemand anders draagt maar niet weet te hebben.

    Apart van :func:`geef_token_uit`, zodat :func:`roteer_token` deze regel kan toetsen
    VOORDAT het oude token eruit gaat. Anders was een geweigerde rotatie het token kwijt.
    """
    _eis_lid(project_name, door)
    doellid = _lid(project_name, naam)
    if doellid is None:
        raise TokenAanvraagGeweigerd(f"'{naam}' is geen lid van dit project.")

    if doellid.type is LidSoort.SERVICE_ACCOUNT:
        _eis_beheerder(project_name, door)
        return settings.TOKEN_MAX_DAGEN_SERVICE_ACCOUNT
    if naam.lower() != door.lower():
        raise TokenAanvraagGeweigerd("Je kunt alleen een token voor jezelf maken.")
    return settings.TOKEN_MAX_DAGEN_PERSOONLIJK


async def geef_token_uit(*, project_name: str, naam: str, tokennaam: str, dagen: int, door: str) -> str:
    """Geef een token uit aan het lid ``naam``. De waarde komt hier een keer vandaan."""
    tokennaam = _eis_tokennaam(tokennaam)
    maximum = _eis_mag_uitgeven(project_name, naam, door)
    verloopt = _vervaldatum(dagen, maximum)
    waarde, hash_hex = nieuw_token()

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        lid = _zoek_lid(data, naam)
        if lid is None:
            raise TokenAanvraagGeweigerd(f"'{naam}' is geen lid van dit project.")
        tokens = lid.setdefault("tokens", [])
        if any(isinstance(t, dict) and t.get("name") == tokennaam for t in tokens):
            raise TokenAanvraagGeweigerd(f"'{naam}' heeft al een token dat '{tokennaam}' heet.")
        tokens.append({"name": tokennaam, "expires": verloopt.isoformat(), "hash": hash_hex})
        return data

    await _wijzig(project_name, change, bericht=f"Token '{tokennaam}' uitgegeven aan '{naam}'", door=door)
    logger.info("Token '%s' uitgegeven aan '%s' in project '%s' door '%s'", tokennaam, naam, project_name, door)
    return waarde


async def trek_token_in(*, project_name: str, naam: str, tokennaam: str, door: str) -> None:
    """Trek een token in door het uit het projectbestand te halen.

    Een beheerder trekt elk token van zijn project in, ook dat van een mens; een mens trekt
    zijn eigen tokens in. Beide zijn lid van het project: een platformbeheerder die dat niet
    is trekt hier niets in. Intrekken is een commit, en daarmee zichtbaar in de historie.
    """
    _eis_lid(project_name, door)
    if naam.lower() != door.lower():
        _eis_beheerder(project_name, door)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        lid = _zoek_lid(data, naam)
        if lid is None:
            return None
        tokens = lid.get("tokens") or []
        over = [t for t in tokens if not (isinstance(t, dict) and t.get("name") == tokennaam)]
        if len(over) == len(tokens):
            return None
        lid["tokens"] = over
        return data

    await _wijzig(project_name, change, bericht=f"Token '{tokennaam}' van '{naam}' ingetrokken", door=door)
    logger.info("Token '%s' van '%s' ingetrokken door '%s'", tokennaam, naam, door)


async def roteer_token(*, project_name: str, naam: str, tokennaam: str, dagen: int, door: str) -> str:
    """Trek dit token in en geef er een nieuw uit onder dezelfde naam.

    Eerst toetsen of het uitgeven mag: intrekken staat een beheerder voor ELK lid toe en
    uitgeven alleen voor een service account, dus zonder deze toets raakte een beheerder
    die het token van een collega roteerde dat token kwijt op een antwoord dat zei dat er
    niets gebeurd was.
    """
    _eis_mag_uitgeven(project_name, naam, door)
    _eis_tokennaam(tokennaam)
    await trek_token_in(project_name=project_name, naam=naam, tokennaam=tokennaam, door=door)
    return await geef_token_uit(project_name=project_name, naam=naam, tokennaam=tokennaam, dagen=dagen, door=door)


def leden_overzicht(project_name: str) -> list[dict[str, Any]]:
    """De leden van dit project met hun tokens, voor de interface en de API.

    Zonder hashes: een hash is geen geheim maar ook geen gegeven dat op een pagina hoort,
    en wie hem heeft kan een geraden token offline toetsen.
    """
    project = get_project_store().get(project_name)
    return [
        {
            "naam": lid.principaal,
            "soort": lid.type.value,
            "rol": lid.role,
            "tokens": [{"naam": t.name, "vervalt": t.expires, "verlopen": is_verlopen(t.expires)} for t in lid.tokens],
        }
        for lid in (project.users or [] if project else [])
    ]


__all__ = [
    "NAAM_PATROON",
    "ROLLEN",
    "TOKEN_PREFIX",
    "TokenAanvraagGeweigerd",
    "geef_token_uit",
    "hash_van",
    "is_projectbeheerder",
    "leden_overzicht",
    "roteer_token",
    "trek_token_in",
    "verwijder_service_account",
    "voeg_service_account_toe",
    "wijzig_rol",
]
