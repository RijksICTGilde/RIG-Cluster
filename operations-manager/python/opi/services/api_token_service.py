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

from opi.core.actor import user_actor
from opi.core.authz.engine import Role, effective_roles
from opi.core.config import settings
from opi.core.leden import LidSoort
from opi.services.project_leden import TOKEN_PREFIX, hash_van, is_verlopen, lid_met_naam, nieuw_token, vervaldatum
from opi.services.project_roles import roles_from_value
from opi.services.project_store import get_project_store

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from opi.services.project_service import ProjectUser

logger = logging.getLogger(__name__)

#: De rollen die een lid kan hebben, als sleutels. Uit het register, zodat een rol die
#: daar bijkomt hier niet vergeten kan worden.
ROLLEN: tuple[str, ...] = tuple(rol.value for rol in Role)

#: De rollen die een nieuw lid vooraf gekozen krijgt (B12). Een voorinvulling, geen vaste
#: toekenning: de admin die het lid toevoegt kiest.
STANDAARD_ROLLEN: tuple[str, ...] = (Role.DEVELOPER.value,)

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


def _rollen(project_name: str, naam: str) -> list[str]:
    """De huidige rollen van dit lid in dit project; leeg als het geen lid is."""
    lid = _lid(project_name, naam)
    return list(lid.roles) if lid is not None else []


def _eis_lid(project_name: str, door: str) -> None:
    """Weiger wie geen lid is van dit project, ook een platformbeheerder.

    Zonder deze eis gaf een intrekking op een BESTAAND project een stille no-op en op een
    onbestaand een 500, en dat verschil leest als "bestaat deze projectnaam".
    """
    if not _rollen(project_name, door):
        raise TokenAanvraagGeweigerd("Je bent geen lid van dit project.")


def is_projectbeheerder(project_name: str, naam: str) -> bool:
    """Of dit lid de leden en tokens van dit project mag beheren.

    Voor de interface, zodat een knop dezelfde bron leest als de weigering erachter: een
    platformbeheerder die geen lid is krijgt hier False en ziet de knoppen dus niet.
    """
    return Role.ADMIN in effective_roles(roles_from_value(_rollen(project_name, naam)))


def _eis_beheerder(project_name: str, door: str) -> None:
    if Role.ADMIN not in effective_roles(roles_from_value(_rollen(project_name, door))):
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


async def voeg_service_account_toe(*, project_name: str, naam: str, rollen: list[str], door: str) -> None:
    """Zet een service account in de ledenlijst van dit project."""
    _eis_beheerder(project_name, door)
    if not NAAM_PATROON.match(naam):
        raise TokenAanvraagGeweigerd(
            "Een naam bestaat uit kleine letters, cijfers en streepjes, en begint met een letter."
        )
    gekozen = schoon_rollen(rollen)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        if _zoek_lid(data, naam) is not None:
            return None
        _leden(data).append({"type": LidSoort.SERVICE_ACCOUNT.value, "name": naam, "roles": gekozen, "tokens": []})
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


def schoon_rollen(rollen: Iterable[str]) -> list[str]:
    """De gekozen rollen als geldige, ontdubbelde, gesorteerde lijst.

    Raises:
        TokenAanvraagGeweigerd: Bij een lege lijst of een rol die niet bestaat.
    """
    gekozen = sorted({str(rol) for rol in rollen if str(rol)})
    if not gekozen:
        raise TokenAanvraagGeweigerd("Kies minstens een rol.")
    onbekend = [rol for rol in gekozen if rol not in ROLLEN]
    if onbekend:
        raise TokenAanvraagGeweigerd(f"Onbekende rol: {', '.join(onbekend)}.")
    return gekozen


def eis_een_admin(data: dict[str, Any]) -> None:
    """Weiger een ledenlijst waarin niemand de rol admin heeft.

    De ENE lezer van die eis: zowel een rolwijziging per lid als de team-wizard, die de
    hele lijst in een keer schrijft, lopen hier langs.

    Raises:
        TokenAanvraagGeweigerd: Als er geen lid met de rol admin overblijft.
    """
    if not any(isinstance(lid, dict) and Role.ADMIN.value in (lid.get("roles") or []) for lid in _leden(data)):
        raise TokenAanvraagGeweigerd("Een project houdt minstens een lid met de rol admin.")


def zet_rollen_in_data(data: dict[str, Any], naam: str, rollen: list[str]) -> dict[str, Any] | None:
    """Zet de rollen van dit lid in de projectdata. None als er niets verandert.

    Apart van :func:`set_member_roles`, omdat die het bestand uitleest en wegschrijft en
    deze stap alleen de data raakt; de taakomgeving van de wijziging hoort niet in de
    guard.

    Raises:
        TokenAanvraagGeweigerd: Als het lid er niet is, of als het project zonder admin
            zou achterblijven.
    """
    lid = _zoek_lid(data, naam)
    if lid is None:
        raise TokenAanvraagGeweigerd(f"'{naam}' is geen lid van dit project.")
    if list(lid.get("roles") or []) == rollen:
        return None
    was = lid.get("roles")
    lid["roles"] = rollen
    try:
        eis_een_admin(data)
    except TokenAanvraagGeweigerd:
        lid["roles"] = was
        raise
    return data


async def set_member_roles(*, project_name: str, naam: str, rollen: list[str], door: str) -> None:
    """Geef een lid andere rollen. Zijn tokens volgen die rollen meteen.

    De ENE schrijfweg voor rollen (ontwerp paragraaf 7): de Team-kaart, de API-route en
    de team-wizard lopen hier langs, zodat de drie niet uit elkaar kunnen lopen op wat
    een geldige rol is of op de eis dat er een admin overblijft.
    """
    _eis_beheerder(project_name, door)
    gekozen = schoon_rollen(rollen)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        return zet_rollen_in_data(data, naam, gekozen)

    await _wijzig(project_name, change, bericht=f"Rollen van '{naam}' zijn nu {gekozen}", door=door)


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
            "rollen": list(lid.roles),
            "rol_labels": lid.rol_labels,
            "tokens": [{"naam": t.name, "vervalt": t.expires, "verlopen": is_verlopen(t.expires)} for t in lid.tokens],
        }
        for lid in (project.users or [] if project else [])
    ]


__all__ = [
    "NAAM_PATROON",
    "ROLLEN",
    "STANDAARD_ROLLEN",
    "TOKEN_PREFIX",
    "TokenAanvraagGeweigerd",
    "eis_een_admin",
    "geef_token_uit",
    "hash_van",
    "is_projectbeheerder",
    "leden_overzicht",
    "roteer_token",
    "schoon_rollen",
    "set_member_roles",
    "trek_token_in",
    "verwijder_service_account",
    "voeg_service_account_toe",
    "zet_rollen_in_data",
]
