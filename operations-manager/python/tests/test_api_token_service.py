"""De regels bij het beheren van leden en tokens: wie mag wat, en wat is geldig.

Deze regels staan in de service en niet in een route, zodat het formulier en de API ze
allebei lopen. Wat hier wordt vastgelegd:

- een beheerder beheert de service accounts van zijn project, een mens alleen zichzelf;
- niemand maakt een token voor een ANDER mens: dat zou een bewijsmiddel zijn dat iemand
  draagt zonder te weten dat het bestaat;
- een token heeft altijd een vervaldatum, en nooit meer dan het platformmaximum;
- intrekken en roteren veranderen het projectbestand, dus het zijn commits.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.core.leden import LidSoort
from opi.services import api_token_service as mod
from opi.services.api_token_service import (
    TokenAanvraagGeweigerd,
    geef_token_uit,
    is_projectbeheerder,
    leden_overzicht,
    roteer_token,
    trek_token_in,
    verwijder_service_account,
    voeg_service_account_toe,
    wijzig_rol,
)
from opi.services.project_leden import TOKEN_PREFIX, hash_van
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser

pytestmark = pytest.mark.asyncio

PROJECT = "bouwput"
VERVALT = "2030-01-01T00:00:00+00:00"
BEHEERDER = "beheerder@rijksoverheid.nl"
DEV = "dev@rijksoverheid.nl"


def _bestand() -> dict[str, Any]:
    return {
        "name": PROJECT,
        "users": [
            {
                "email": BEHEERDER,
                "role": "admin",
                "tokens": [{"name": "cli", "expires": "2030-01-01T00:00:00+00:00", "hash": hash_van("zad_oud")}],
            },
            {"email": DEV, "role": "developer"},
            {"type": "service-account", "name": "bouwstraat", "role": "developer", "tokens": []},
        ],
    }


def _summary() -> ProjectSummary:
    vervalt = datetime.now(UTC) + timedelta(days=365 * 5)
    return ProjectSummary(
        name=PROJECT,
        filename=f"projects/{PROJECT}.yaml",
        users=[
            ProjectUser(
                email=BEHEERDER,
                role="admin",
                tokens=[ProjectToken(name="cli", expires=vervalt, hash=hash_van("zad_oud"))],
            ),
            ProjectUser(email=DEV, role="developer"),
            ProjectUser(type=LidSoort.SERVICE_ACCOUNT, name="bouwstraat", role="developer"),
        ],
    )


@pytest.fixture
def store():
    """Een store die ``mutate`` op een echte dict uitvoert, zodat de change-functie telt."""
    bestand = _bestand()
    nep = AsyncMock()

    async def mutate(name: str, change, *, message, actor):
        # De echte store geeft de change-functie een kopie van de verse staat; hier ook,
        # zodat een change die halverwege weigert niets achterlaat.
        resultaat = change(copy.deepcopy(bestand))
        if resultaat is not None:
            bestand.clear()
            bestand.update(resultaat)
        # Voor tests die het stille no-op-pad toetsen: de echte store doet dan geen
        # commit (``MutationResult.committed=False``); dat heeft deze nep niet, dus de
        # laatste uitkomst van ``change`` zelf is hier het signaal.
        nep.laatste_change_resultaat = resultaat
        return resultaat

    nep.get = lambda naam: _summary() if naam == PROJECT else None
    nep.mutate = AsyncMock(side_effect=mutate)
    with patch.object(mod, "get_project_store", return_value=nep):
        yield bestand


def _lid(bestand: dict[str, Any], naam: str) -> dict[str, Any] | None:
    for lid in bestand["users"]:
        if (lid.get("name") or lid.get("email")) == naam:
            return lid
    return None


# --- service accounts ------------------------------------------------------


async def test_een_beheerder_voegt_een_service_account_toe(store) -> None:
    await voeg_service_account_toe(project_name=PROJECT, naam="nachtploeg", rol="developer", door=BEHEERDER)

    account = _lid(store, "nachtploeg")
    assert account == {"type": "service-account", "name": "nachtploeg", "role": "developer", "tokens": []}


async def test_een_ontwikkelaar_voegt_er_geen_toe(store) -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="beheerder"):
        await voeg_service_account_toe(project_name=PROJECT, naam="nachtploeg", rol="admin", door=DEV)


async def test_een_naam_die_geen_dns_label_is_wordt_geweigerd(store) -> None:
    """De naam reist mee in de actor en in de git-trailer."""
    with pytest.raises(TokenAanvraagGeweigerd, match="kleine letters"):
        await voeg_service_account_toe(project_name=PROJECT, naam="Nacht Ploeg", rol="admin", door=BEHEERDER)


async def test_een_onbekende_rol_bij_het_toevoegen_wordt_geweigerd(store) -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="Onbekende rol"):
        await voeg_service_account_toe(project_name=PROJECT, naam="nachtploeg", rol="superuser", door=BEHEERDER)


async def test_een_service_account_die_al_bestaat_toevoegen_is_een_stille_no_op(store) -> None:
    """Geen fout, geen tweede rij, en geen commit: de store ziet een no-op ``None`` terug."""
    voor = copy.deepcopy(_lid(store, "bouwstraat"))
    nep = mod.get_project_store()

    await voeg_service_account_toe(project_name=PROJECT, naam="bouwstraat", rol="admin", door=BEHEERDER)

    rijen = [lid for lid in store["users"] if lid.get("name") == "bouwstraat"]
    assert rijen == [voor]
    assert nep.laatste_change_resultaat is None


async def test_een_service_account_verwijderen_neemt_zijn_tokens_mee(store) -> None:
    store["users"][2]["tokens"] = [{"name": "pijplijn", "expires": "2030-01-01T00:00:00+00:00", "hash": "f" * 64}]

    await verwijder_service_account(project_name=PROJECT, naam="bouwstraat", door=BEHEERDER)

    assert _lid(store, "bouwstraat") is None


async def test_een_onbestaand_service_account_verwijderen_is_een_stille_no_op(store) -> None:
    nep = mod.get_project_store()
    await verwijder_service_account(project_name=PROJECT, naam="bestaat-niet", door=BEHEERDER)

    assert nep.mutate.await_count == 1
    assert len(store["users"]) == 3
    assert nep.laatste_change_resultaat is None


async def test_een_mens_verwijderen_via_deze_route_is_een_stille_no_op(store) -> None:
    """Deze route haalt alleen een SERVICE ACCOUNT weg; een mens met die naam blijft staan."""
    nep = mod.get_project_store()
    await verwijder_service_account(project_name=PROJECT, naam=DEV, door=BEHEERDER)

    assert _lid(store, DEV) is not None
    assert nep.laatste_change_resultaat is None


async def test_een_rol_wijzigen_raakt_alleen_de_rol(store) -> None:
    await wijzig_rol(project_name=PROJECT, naam="bouwstraat", rol="admin", door=BEHEERDER)

    assert _lid(store, "bouwstraat")["role"] == "admin"


async def test_dezelfde_rol_opnieuw_zetten_is_een_stille_no_op(store) -> None:
    nep = mod.get_project_store()
    await wijzig_rol(project_name=PROJECT, naam="bouwstraat", rol="developer", door=BEHEERDER)

    assert nep.mutate.await_count == 1
    assert _lid(store, "bouwstraat")["role"] == "developer"
    assert nep.laatste_change_resultaat is None


async def test_een_onbekende_rol_wordt_geweigerd(store) -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="Onbekende rol"):
        await wijzig_rol(project_name=PROJECT, naam="bouwstraat", rol="superuser", door=BEHEERDER)


async def test_een_onbestaand_lid_een_rol_geven_wordt_geweigerd(store) -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="geen lid"):
        await wijzig_rol(project_name=PROJECT, naam="bestaat-niet", rol="admin", door=BEHEERDER)


# --- wie mag beheren --------------------------------------------------------


def test_een_beheerder_mag_leden_en_tokens_beheren(store) -> None:
    assert is_projectbeheerder(PROJECT, BEHEERDER) is True


def test_een_ontwikkelaar_mag_dat_niet(store) -> None:
    assert is_projectbeheerder(PROJECT, DEV) is False


def test_een_platformbeheerder_die_geen_lid_is_mag_dat_ook_niet(store) -> None:
    """Zonder deze check zag een platformbeheerder beheerknoppen die altijd weigeren
    (het tabblad Team leest deze functie om te kiezen of hij ze toont)."""
    assert is_projectbeheerder(PROJECT, "platformbeheerder@rijksoverheid.nl") is False


def test_een_onbestaand_project_heeft_ook_geen_beheerder(store) -> None:
    assert is_projectbeheerder("bestaat-niet", BEHEERDER) is False


# --- tokens ----------------------------------------------------------------


async def test_een_beheerder_geeft_een_service_account_een_token(store) -> None:
    waarde = await geef_token_uit(
        project_name=PROJECT, naam="bouwstraat", tokennaam="pijplijn", dagen=30, door=BEHEERDER
    )

    assert waarde.startswith(TOKEN_PREFIX)
    token = _lid(store, "bouwstraat")["tokens"][0]
    assert token["name"] == "pijplijn"
    assert token["hash"] == hash_van(waarde)
    assert waarde not in str(store), "de waarde zelf hoort nergens in het bestand te staan"


async def test_een_mens_geeft_zichzelf_een_token(store) -> None:
    waarde = await geef_token_uit(project_name=PROJECT, naam=DEV, tokennaam="cli", dagen=30, door=DEV)

    assert _lid(store, DEV)["tokens"][0]["hash"] == hash_van(waarde)


async def test_niemand_maakt_een_token_voor_een_ander_mens(store) -> None:
    """Ook een beheerder niet: dat zou een bewijsmiddel zijn dat iemand draagt zonder te
    weten dat het bestaat."""
    with pytest.raises(TokenAanvraagGeweigerd, match="voor jezelf"):
        await geef_token_uit(project_name=PROJECT, naam=DEV, tokennaam="cli", dagen=30, door=BEHEERDER)


async def test_een_ontwikkelaar_geeft_geen_service_accounttoken_uit(store) -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="beheerder"):
        await geef_token_uit(project_name=PROJECT, naam="bouwstraat", tokennaam="pijplijn", dagen=30, door=DEV)


async def test_een_token_boven_het_platformmaximum_wordt_geweigerd(store) -> None:
    """Het maximum is platformbeleid, geen keuze van wie het token aanvraagt."""
    with pytest.raises(TokenAanvraagGeweigerd, match="hoogstens"):
        await geef_token_uit(project_name=PROJECT, naam=DEV, tokennaam="cli", dagen=9999, door=DEV)


async def test_een_token_van_nul_dagen_bestaat_niet(store) -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="minstens een dag"):
        await geef_token_uit(project_name=PROJECT, naam=DEV, tokennaam="cli", dagen=0, door=DEV)


async def test_een_tokennaam_met_een_rare_letter_wordt_geweigerd(store) -> None:
    """De naam komt op het scherm en in het logboek terecht, dus geen vrije tekst."""
    with pytest.raises(TokenAanvraagGeweigerd, match="korte naam"):
        await geef_token_uit(project_name=PROJECT, naam=DEV, tokennaam="cli/<script>", dagen=30, door=DEV)


async def test_twee_tokens_met_dezelfde_naam_kan_niet(store) -> None:
    """Anders is er geen manier om er een van de twee in te trekken."""
    with pytest.raises(TokenAanvraagGeweigerd, match="al een token"):
        await geef_token_uit(project_name=PROJECT, naam=BEHEERDER, tokennaam="cli", dagen=30, door=BEHEERDER)


async def test_intrekken_haalt_het_token_uit_het_bestand(store) -> None:
    await trek_token_in(project_name=PROJECT, naam=BEHEERDER, tokennaam="cli", door=BEHEERDER)

    assert _lid(store, BEHEERDER)["tokens"] == []


async def test_een_beheerder_trekt_ook_het_token_van_een_mens_in(store) -> None:
    """Offboarding moet kunnen zonder op die persoon te wachten."""
    nep = mod.get_project_store()
    await trek_token_in(project_name=PROJECT, naam=BEHEERDER, tokennaam="cli", door=BEHEERDER)
    assert nep.mutate.await_count == 1


async def test_een_ontwikkelaar_trekt_andermans_token_niet_in(store) -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="beheerder"):
        await trek_token_in(project_name=PROJECT, naam=BEHEERDER, tokennaam="cli", door=DEV)


async def test_intrekken_bij_een_lid_dat_niet_bestaat_is_een_stille_no_op(store) -> None:
    """De beheerder is zelf lid (de grens die ``_eis_lid`` toetst); het DOEL van deze
    intrekking bestaat niet meer, en dat geeft geen fout, alleen geen wijziging."""
    nep = mod.get_project_store()
    await trek_token_in(project_name=PROJECT, naam="vertrokken-collega", tokennaam="cli", door=BEHEERDER)

    assert nep.mutate.await_count == 1
    assert len(store["users"]) == 3
    assert nep.laatste_change_resultaat is None


async def test_intrekken_van_een_tokennaam_die_niet_bestaat_is_een_stille_no_op(store) -> None:
    voor = copy.deepcopy(_lid(store, BEHEERDER)["tokens"])
    nep = mod.get_project_store()

    await trek_token_in(project_name=PROJECT, naam=BEHEERDER, tokennaam="bestaat-niet", door=BEHEERDER)

    assert _lid(store, BEHEERDER)["tokens"] == voor
    assert nep.laatste_change_resultaat is None


async def test_een_niet_lid_trekt_ook_zijn_eigen_token_hier_niet_in(store) -> None:
    """De tak ``naam == door`` sloeg elke controle over, en dat was een projectnaam-orakel.

    Een niet-lid kreeg op een BESTAAND project een stille no-op (303) en op een onbestaand
    een ontsnapte ``ProjectNotFoundError`` (500). Dat verschil zegt of een projectnaam
    bestaat, precies de grendel die ``herken_principaal`` dichthoudt.
    """
    vreemde = "vreemde@rijksoverheid.nl"
    nep = mod.get_project_store()
    with pytest.raises(TokenAanvraagGeweigerd, match="geen lid"):
        await trek_token_in(project_name=PROJECT, naam=vreemde, tokennaam="x", door=vreemde)

    assert nep.mutate.await_count == 0


async def test_een_onbestaand_project_weigert_net_zo(store) -> None:
    """Zelfde melding als een bestaand project waar je geen lid van bent: geen orakel."""
    with pytest.raises(TokenAanvraagGeweigerd, match="geen lid"):
        await trek_token_in(project_name="bestaat-niet", naam=BEHEERDER, tokennaam="cli", door=BEHEERDER)


async def test_een_beheerder_die_andermans_token_roteert_maakt_het_niet_kwijt(store) -> None:
    """Intrekken mag een beheerder voor ELK lid, uitgeven alleen voor een service account.

    Zonder de toets vooraf trok de rotatie het token van de collega eerst in en viel ze
    daarna op de weigering: een 403 die zegt dat er niets gebeurd is, met het token weg.
    """
    laptop = {"name": "laptop", "expires": VERVALT, "hash": "c" * 64}
    store["users"][1]["tokens"] = [laptop]
    nep = mod.get_project_store()

    with pytest.raises(TokenAanvraagGeweigerd, match="alleen een token voor jezelf"):
        await roteer_token(project_name=PROJECT, naam=DEV, tokennaam="laptop", dagen=30, door=BEHEERDER)

    assert _lid(store, DEV)["tokens"] == [laptop]
    assert nep.mutate.await_count == 0


async def test_een_mens_roteert_zijn_eigen_token_en_houdt_de_naam(store) -> None:
    """Het gewone pad: dezelfde tokennaam blijft staan, maar met een nieuwe waarde en hash."""
    oud = {"name": "laptop", "expires": VERVALT, "hash": "c" * 64}
    store["users"][1]["tokens"] = [oud]

    waarde = await roteer_token(project_name=PROJECT, naam=DEV, tokennaam="laptop", dagen=30, door=DEV)

    tokens = _lid(store, DEV)["tokens"]
    assert [t["name"] for t in tokens] == ["laptop"]
    assert tokens[0]["hash"] == hash_van(waarde)
    assert tokens[0]["hash"] != oud["hash"]


async def test_een_beheerder_roteert_het_token_van_een_service_account(store) -> None:
    """Uitgeven voor een service account mag een beheerder wel, dus de rotatie slaagt."""
    oud = {"name": "pijplijn", "expires": VERVALT, "hash": "d" * 64}
    _lid(store, "bouwstraat")["tokens"] = [oud]

    waarde = await roteer_token(project_name=PROJECT, naam="bouwstraat", tokennaam="pijplijn", dagen=30, door=BEHEERDER)

    tokens = _lid(store, "bouwstraat")["tokens"]
    assert [t["name"] for t in tokens] == ["pijplijn"]
    assert tokens[0]["hash"] == hash_van(waarde)


# --- overzicht -------------------------------------------------------------


async def test_het_overzicht_draagt_geen_hashes(store) -> None:
    """Wie een hash heeft kan een geraden token offline toetsen."""
    overzicht = leden_overzicht(PROJECT)

    assert [lid["naam"] for lid in overzicht] == [BEHEERDER, DEV, "bouwstraat"]
    assert "hash" not in str(overzicht)
    assert overzicht[0]["tokens"][0]["verlopen"] is False
