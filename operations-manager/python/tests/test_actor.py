"""Tests for opi.core.actor: de server-side bepaalde handelende partij."""

from __future__ import annotations

import asyncio
import contextvars

from opi.core.actor import (
    ACTOR_PLATFORM_API_KEY,
    ACTOR_UNKNOWN,
    Actor,
    ActorKind,
    actor_from_stored,
    get_actor,
    project_api_key_actor,
    scheduler_actor,
    set_actor,
    user_actor,
)


def test_ongezette_actor_valt_terug_op_onbekend() -> None:
    """Los van de conftest-fixture: een Context waarin niemand ooit set_actor riep."""
    assert contextvars.Context().run(get_actor) == ACTOR_UNKNOWN


def test_set_actor_komt_terug_uit_get_actor() -> None:
    set_actor(user_actor("robbert@example.org"))
    assert get_actor() == Actor(ActorKind.USER, "robbert@example.org")


def test_soort_en_identiteit_staan_los_van_elkaar() -> None:
    """Een lezer (de gebeurtenissentabel) mag de soort niet uit tekst hoeven halen."""
    actor = project_api_key_actor("algor-odc")
    assert actor.kind is ActorKind.PROJECT_API_KEY
    assert actor.identity == "algor-odc"


def test_project_api_key_actor_bevat_de_projectnaam() -> None:
    assert str(project_api_key_actor("algor-odc")) == "project-api-key:algor-odc"


def test_scheduler_actor_bevat_de_naam() -> None:
    assert str(scheduler_actor("backup")) == "scheduler:backup"


def test_een_gebruiker_wordt_als_kaal_e_mailadres_geschreven() -> None:
    """Zo komt de waarde heen en terug door ``created_by``, waar hij met een
    e-mailadres wordt vergeleken."""
    assert str(user_actor("iemand@example.org")) == "iemand@example.org"


def test_een_sleutel_zonder_identiteit_schrijft_alleen_zijn_soort() -> None:
    assert str(ACTOR_PLATFORM_API_KEY) == "platform-api-key"
    assert str(ACTOR_UNKNOWN) == "onbekend"


def test_vaste_actor_waarden_zijn_onderling_verschillend() -> None:
    """Een trailer is alleen naar de bron te herleiden als elke vaste waarde uniek is."""
    assert len({ACTOR_UNKNOWN, ACTOR_PLATFORM_API_KEY}) == 2


def test_opgeslagen_waarden_komen_terug_als_soort_en_identiteit() -> None:
    assert actor_from_stored("iemand@example.org") == user_actor("iemand@example.org")
    assert actor_from_stored("project-api-key:demo") == project_api_key_actor("demo")
    assert actor_from_stored("scheduler:backup") == scheduler_actor("backup")
    assert actor_from_stored("platform-api-key") == ACTOR_PLATFORM_API_KEY
    assert actor_from_stored(None) == ACTOR_UNKNOWN


def test_een_opgeslagen_waarde_zonder_actorvorm_krijgt_geen_soort_toegedicht() -> None:
    """De bestaande producenten van ``created_by`` schrijven ook "API" en
    "backup-scheduler"; die zijn geen gebruiker en geen sleutel."""
    assert actor_from_stored("API") == Actor(ActorKind.UNKNOWN, "API")
    assert str(actor_from_stored("API")) == "API"


def test_elke_opgeslagen_actor_schrijft_zichzelf_onveranderd_terug() -> None:
    for waarde in ("iemand@example.org", "project-api-key:demo", "scheduler:backup", "platform-api-key", "API"):
        assert str(actor_from_stored(waarde)) == waarde


def test_gelijktijdige_taken_zien_elk_hun_eigen_actor() -> None:
    """Een contextvar, geen module-brede variabele: twee taken die tegelijk op de event
    loop draaien mogen elkaars actor niet overschrijven (plan RC-284, punt 1)."""
    results: dict[str, Actor] = {}

    async def _zet_en_lees(naam: str) -> None:
        set_actor(user_actor(naam))
        await asyncio.sleep(0)  # geef de andere taak de kans om te draaien
        results[naam] = get_actor()

    async def _main() -> None:
        await asyncio.gather(_zet_en_lees("taak-a"), _zet_en_lees("taak-b"))

    asyncio.run(_main())

    assert results == {"taak-a": user_actor("taak-a"), "taak-b": user_actor("taak-b")}
