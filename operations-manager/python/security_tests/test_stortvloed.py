"""Een reeks foute sleutels en een ruwe stortvloed lopen tegen een goedkope weigering.

Een foute ``X-API-Key`` kost OPI nu nog een volledige verwerking per poging: dat maakt zowel
het gokken van een sleutel als het platleggen van de pod goedkoop voor de aanvaller en duur
voor ons. Deze proef meet of er na een drempel een snelle 429 komt, of de weigering goedkoop
is, en of de pod de stortvloed overleeft.

De tegenproef staat erbij: de subdomeincontrole HEEFT een limiter, dus daar hoort de proef een
429 te zien. Zonder die tegenproef zou "geen 429 gevonden" ook kunnen betekenen dat de proef
geen 429 kan zien.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, NamedTuple

import httpx

from security_tests.baseline import toets_tegen_baseline
from security_tests.conftest import proefclient, sleutelclient

if TYPE_CHECKING:
    from security_tests.doel import Doel

PROEF = "stortvloed"

#: Zoveel foute pogingen mag een client doen voordat er een weigering op de deur hoort te staan.
DREMPEL = 30

#: Een weigering hoort niets te kosten. Hierboven handelt OPI de poging af in plaats van hem af
#: te wijzen, en dan is een stortvloed nog steeds duur.
GOEDKOOP_SECONDEN = 0.25

#: Het aantal verzoeken van de ruwe stortvloed, en hoeveel er tegelijk gaan.
STORTVLOED = 200
GELIJKTIJDIG = 20

#: Per poging, en korter dan het gewone budget: een reeks van dertig pogingen die allemaal op
#: de klok aflopen duurt anders langer dan de proef zelf.
TIJDBUDGET_POGING = 3.0

#: De status van een poging die geen antwoord opleverde: een tijdbudget dat afliep, of een
#: verbinding die de server verbrak. In de wegwerp-sandbox gebeurt dat op de routes die een
#: database zoeken; het is dus geen uitspraak over de weigering.
GEEN_ANTWOORD = 0


class Poging(NamedTuple):
    """Wat een poging opleverde: de status, de Retry-After, en wat hij kostte."""

    status: int
    retry_after: str
    duur: float


def doe_pogingen(client: httpx.Client, pad: str, aantal: int) -> list[Poging]:
    """Doe dezelfde aanroep ``aantal`` keer en meet elke poging."""
    pogingen = []
    for _ in range(aantal):
        begin = time.monotonic()
        try:
            antwoord = client.get(pad)
        except httpx.HTTPError:
            pogingen.append(Poging(GEEN_ANTWOORD, "", time.monotonic() - begin))
            continue
        pogingen.append(Poging(antwoord.status_code, antwoord.headers.get("retry-after", ""), time.monotonic() - begin))
    return pogingen


def beoordeel_reeks(pogingen: list[Poging], kenmerk: str) -> dict[str, str]:
    """De bevindingen over een reeks pogingen, als kenmerk naar melding."""
    bevindingen: dict[str, str] = {}
    geweigerd = [poging for poging in pogingen if poging.status == 429]
    if not geweigerd:
        bevindingen[f"{kenmerk}/geen-429"] = (
            f"{len(pogingen)} pogingen worden allemaal vol verwerkt; na {DREMPEL} hoort er een 429 te staan"
        )
    elif not any(poging.retry_after for poging in geweigerd):
        bevindingen[f"{kenmerk}/geen-retry-after"] = "de 429 zegt niet wanneer de client terug mag komen"
    traag = max((poging.duur for poging in pogingen if poging.status in (401, 429)), default=0.0)
    if traag > GOEDKOOP_SECONDEN:
        bevindingen[f"{kenmerk}/traag"] = f"de langzaamste weigering kostte {traag:.2f}s"
    return bevindingen


def test_een_reeks_foute_sleutels_loopt_tegen_een_weigering(doel: Doel, proefproject: str) -> None:
    """Faalconditie: elke foute poging wordt vol verwerkt, of de weigering is duur."""
    pad = f"/api/v2/projects/{proefproject}/deployments"
    with proefclient(doel, timeout=TIJDBUDGET_POGING, headers={"X-API-Key": "fout"}) as client:
        pogingen = doe_pogingen(client, pad, DREMPEL)
    assert all(poging.status in (401, 429) for poging in pogingen), f"statussen: {[p.status for p in pogingen]}"
    meldingen = toets_tegen_baseline(PROEF, beoordeel_reeks(pogingen, "foute-sleutel"), doel.soort)
    assert not meldingen, "\n".join(meldingen)


def test_de_limiter_die_er_is_wordt_gezien(doel: Doel, proefproject: str) -> None:
    """Tegenproef: op de subdomeincontrole zit wel een limiter, en die hoort de proef te zien."""
    project = proefproject
    pad = f"/api/v2/projects/{project}/subdomains/check/proefnaam?base_domain=rijksapp.nl"
    # Deze route zoekt eerst in de database, die in de wegwerp-sandbox niet bestaat; de
    # limiter staat ervoor. Een poging die op de klok afloopt is dus geen uitspraak, een 429 wel.
    with sleutelclient(doel, project) as client:
        client.timeout = TIJDBUDGET_POGING
        pogingen = doe_pogingen(client, pad, DREMPEL)
    assert any(poging.status == 429 for poging in pogingen), (
        "geen 429 op een route die wel een limiter heeft; de proef kan een weigering niet zien"
    )


def test_een_stortvloed_laat_de_pod_staan(doel: Doel) -> None:
    """Faalconditie: de pod antwoordt niet meer, of pas buiten het tijdbudget."""

    def een_verzoek(_: int) -> int:
        with proefclient(doel) as client:
            return client.get("/health").status_code

    with ThreadPoolExecutor(max_workers=GELIJKTIJDIG) as pool:
        statussen = list(pool.map(een_verzoek, range(STORTVLOED)))
    assert all(status in (200, 429, 503) for status in statussen), f"onverwachte statussen: {set(statussen)}"

    with proefclient(doel) as client:
        begin = time.monotonic()
        antwoord = client.get("/health")
        duur = time.monotonic() - begin
    assert antwoord.status_code == 200, f"de pod antwoordt met {antwoord.status_code} na de stortvloed"
    assert duur < 2.0, f"de pod deed {duur:.1f}s over een healthcheck na de stortvloed"


def test_de_stortvloedproef_merkt_een_ontbrekende_weigering_op() -> None:
    """Negatief voorbeeld: zo ziet een reeks eruit die de proef rood hoort te maken."""
    alleen_401 = [Poging(401, "", 0.01)] * DREMPEL
    assert "foute-sleutel/geen-429" in beoordeel_reeks(alleen_401, "foute-sleutel")

    met_429 = [Poging(401, "", 0.01)] * 10 + [Poging(429, "60", 0.01)] * 5
    assert beoordeel_reeks(met_429, "foute-sleutel") == {}

    zonder_retry = [Poging(429, "", 0.01)]
    assert "foute-sleutel/geen-retry-after" in beoordeel_reeks(zonder_retry, "foute-sleutel")

    traag = [Poging(429, "60", 1.5)]
    assert "foute-sleutel/traag" in beoordeel_reeks(traag, "foute-sleutel")
