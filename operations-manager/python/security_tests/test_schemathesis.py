"""Property-based verzoeken uit de OpenAPI van OPI, in drie herkenningen.

Schemathesis leest ``/openapi.json`` en genereert per operatie verzoeken die aan het schema
voldoen maar verder alles proberen. Dat raakt de routes die de gerichte proeven met een lege
body niet voorbij de bodyvalidatie krijgen (zie ``test_auth_dekking.py``).

Wat hier wordt beoordeeld, hangt af van de herkenning:

* **ongeauthenticeerd** en **met de sleutel van een ander project**: niets mag door. Een 2xx
  betekent dat een vreemde een actie heeft gedaan, een 5xx dat zijn verzoek de applicatie
  heeft geraakt in plaats van de grendel. Dit is ook de autorisatie-matrix: de sleutel van
  project A mag niets in project B.
* **met de eigen sleutel** en **als ZAD-admin**: hier hoort het verzoek juist door te komen,
  dus wordt het antwoord tegen het eigen responsschema gehouden, en gelezen op een lek.
  Een 5xx is in de wegwerp-sandbox GEEN uitspraak: er is geen database, geen cluster en geen
  registry, dus een route die daarop leunt valt daar om en niet op zijn invoer.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, settings
from tests.test_elke_route_heeft_een_herkenning import PUBLIEKE_ROUTES

from security_tests.doel import OnveiligDoel, huidig_doel
from security_tests.schemathesis_oordeel import beoordeel_weigering
from security_tests.test_web_payloads import bevat_lek

if TYPE_CHECKING:
    from schemathesis import Case

try:
    import schemathesis

    DOEL = huidig_doel()
    SCHEMA = schemathesis.openapi.from_url(f"{DOEL.url}/openapi.json")
except (OnveiligDoel, OSError) as fout:  # pragma: no cover - zonder sandbox is er niets te meten
    pytest.skip(f"geen proefsandbox: {fout}", allow_module_level=True)

#: Het aantal gegenereerde verzoeken per operatie. Laag, want het gaat hier om 155 operaties
#: maal vier herkenningen; met ZAD_PROEF_VOORBEELDEN hoger kun je langer laten zoeken.
VOORBEELDEN = int(os.environ.get("ZAD_PROEF_VOORBEELDEN", "3"))

PROEFINSTELLINGEN = settings(
    max_examples=VOORBEELDEN,
    deadline=None,
    # Hypothesis' gezondheidschecks gaan over de kwaliteit van een datageneratie; hier doet elk
    # voorbeeld een echt HTTP-verzoek, dus traag en te weinig variatie zijn geen fout.
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.filter_too_much,
        HealthCheck.function_scoped_fixture,
    ],
)

#: Het project waar de brede fuzz op los mag. Het andere blijft heel voor de gerichte proeven.
#: Een doel zonder proefprojecten (een sandbox in het cluster die er geen meekreeg) heeft hier
#: niets, en dan slaan de passen met een sleutel zich over in plaats van op een KeyError om te
#: vallen.
FUZZPROJECT = DOEL.projecten[-1] if DOEL.projecten else ""
VREEMD_PROJECT = DOEL.projecten[0] if DOEL.projecten else ""

#: De headers waarmee schemathesis zelf een herkenning meegeeft: het securityschema uit de
#: OpenAPI. Zonder ze weg te halen is "ongeauthenticeerd" een verzoek met een verzonnen sleutel.
HERKENNINGSHEADERS = ("x-api-key", "authorization", "cookie")


def _niet_geremd(status: int) -> None:
    """Een 429 voor een aanroeper die juist door hoort te komen is niets gemeten.

    De ratelimiter weigert voor de handler, dus er is geen antwoord om tegen het schema te
    houden. De brede fuzz doet duizenden verzoeken, dus dit komt voor.
    """
    if status == 429:
        pytest.skip("de ratelimiter weigerde dit verzoek, dus er is geen antwoord gemeten")


def _is_publiek(case: Case) -> bool:
    """Of deze operatie bewust publiek is, volgens dezelfde lijst als de statische toets."""
    return (case.method.upper(), case.operation.path) in PUBLIEKE_ROUTES


def _zonder_herkenning(case: Case) -> None:
    """Haal de herkenning die schemathesis uit het securityschema genereerde weer weg."""
    headers: dict[str, Any] = case.headers or {}
    for naam in list(headers):
        if naam.lower() in HERKENNINGSHEADERS:
            del headers[naam]
    case.headers = headers


def _zonder_project(case: Case) -> bool:
    """Of deze operatie geen project in haar pad heeft, en dus buiten de matrix valt."""
    return "{project_name}" not in case.operation.path


def _vraagt_een_proefproject() -> None:
    """Een pas met een projectsleutel vraagt twee proefprojecten op dit doel."""
    if len(DOEL.projecten) < 2:
        pytest.skip("dit doel heeft geen twee proefprojecten met een sleutel (ZAD_PROEF_SLEUTELS)")


def _op_fuzzproject(case: Case) -> None:
    """Richt het verzoek op het fuzzproject in plaats van op een verzonnen naam."""
    parameters: dict[str, Any] = case.path_parameters or {}
    parameters["project_name"] = FUZZPROJECT
    case.path_parameters = parameters


@PROEFINSTELLINGEN
@SCHEMA.parametrize()
def test_ongeauthenticeerd_komt_niets_door(case: Case) -> None:
    """Faalconditie: een naamloos verzoek wordt uitgevoerd (2xx) of raakt de applicatie (5xx)."""
    if _is_publiek(case):
        pytest.skip("bewust publieke route (PUBLIEKE_ROUTES)")
    _zonder_herkenning(case)
    antwoord = case.call()
    melding = beoordeel_weigering(antwoord.status_code, case.operation.label, "een naamloze aanroeper")
    assert melding is None, melding


@PROEFINSTELLINGEN
@SCHEMA.parametrize()
def test_de_sleutel_van_een_ander_project_komt_niets_door(case: Case) -> None:
    """Faalconditie: de sleutel van project A doet iets in project B.

    Dit is de autorisatie-matrix: beide proefprojecten hebben hun eigen sleutel, en het
    verzoek gaat met de sleutel van het ene naar het pad van het andere.
    """
    if _zonder_project(case):
        pytest.skip("geen projectpad, dus geen matrix")
    _vraagt_een_proefproject()
    _zonder_herkenning(case)
    _op_fuzzproject(case)
    vreemde_sleutel = DOEL.sleutels[VREEMD_PROJECT]
    antwoord = case.call(headers={"X-API-Key": vreemde_sleutel})
    melding = beoordeel_weigering(
        antwoord.status_code, case.operation.label, f"de sleutel van {VREEMD_PROJECT} op {FUZZPROJECT}"
    )
    assert melding is None, melding


@PROEFINSTELLINGEN
@SCHEMA.parametrize()
def test_de_eigen_sleutel_krijgt_zijn_eigen_responsschema(case: Case) -> None:
    """Faalconditie: het antwoord wijkt af van het schema, of lekt een stacktrace of een pad."""
    if _zonder_project(case) or case.method.upper() != "GET":
        pytest.skip("alleen leesacties op het eigen project")
    _vraagt_een_proefproject()
    _zonder_herkenning(case)
    _op_fuzzproject(case)
    antwoord = case.call(headers={"X-API-Key": DOEL.sleutels[FUZZPROJECT]})
    _niet_geremd(antwoord.status_code)
    lek = bevat_lek(antwoord.text)
    assert lek is None, f"{case.operation.label}: {lek}"
    case.validate_response(antwoord, checks=[schemathesis.checks.response_schema_conformance])


@PROEFINSTELLINGEN
@SCHEMA.parametrize()
def test_de_zad_admin_krijgt_zijn_eigen_responsschema(case: Case) -> None:
    """Dezelfde toets met een ingelogde ZAD-admin in plaats van een projectsleutel."""
    if case.method.upper() != "GET":
        pytest.skip("alleen leesacties")
    if "zad-admin" not in DOEL.sessies:
        pytest.skip("dit doel heeft geen ondertekende sessie voor rol zad-admin")
    _zonder_herkenning(case)
    antwoord = case.call(cookies={"session": DOEL.sessies["zad-admin"]})
    _niet_geremd(antwoord.status_code)
    lek = bevat_lek(antwoord.text)
    assert lek is None, f"{case.operation.label}: {lek}"
    case.validate_response(antwoord, checks=[schemathesis.checks.response_schema_conformance])
