"""Het document noemt per endpoint de beveiliging die dat endpoint werkelijk wil.

DE MELDING (RC-113, bevinding 1)

``/openapi.json`` hing aan alle 95 v2-operaties het schema ``APIKeyHeader``, ook aan
``POST /api/v2/projects``. Dat endpoint kan geen projectsleutel gebruiken, want het project
bestaat op dat moment nog niet; het wil een SSO-token. Een bearer-schema kwam in het
document helemaal niet voor.

WAAROM DAT MEER IS DAN EEN SCHOONHEIDSFOUT

Het gedrag klopte, alleen de beschrijving niet. Maar het document is hier de
machineleesbare afspraak: wie er een client uit genereert stuurt ``X-API-Key`` en krijgt
een 401 die nergens uit te verklaren valt, want de documentatie zegt dat het zo hoort.
Empirisch bevestigd in RC-113: met de admin-sleutel 401, met een zad-cli-token 202.

DE REGEL

Een route die ``@validate_user_token`` draagt, staat in het document met ``BearerToken``;
elke andere ``/api/``-route met ``APIKeyHeader``. Dat wordt uit de route zelf gelezen (de
markering ``REQUIRES_USER_TOKEN``) en niet uit een lijst met paden, zodat de volgende route
die deze decorator krijgt vanzelf goed gedocumenteerd staat.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.api.user_token_auth import REQUIRES_USER_TOKEN
from opi.server import app

#: De twee endpoints die vandaag een bearertoken willen. Ze staan hier voluit omdat ze het
#: geval uit de melding zijn; de test daaronder werkt generiek en vangt ook de volgende.
BEARER_ENDPOINTS = [("/api/v2/projects", "get"), ("/api/v2/projects", "post")]


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    """Het document zoals de applicatie het opbouwt.

    Niet over HTTP: zonder draaiende diensten antwoordt de onderhoudsmiddleware met een
    opstartpagina in plaats van het document. Dat dit dezelfde inhoud is als wat
    ``/openapi.json`` uitdeelt, ligt vast in ``test_openapi_config_choices.py``.
    """
    return app.openapi()


def test_beide_schemas_staan_in_het_document(spec: dict[str, Any]) -> None:
    schemes = spec["components"]["securitySchemes"]

    assert schemes["APIKeyHeader"]["name"] == "X-API-Key"
    # http+bearer is de standaardvorm; een client weet daarmee zelf welke header het wordt.
    assert schemes["BearerToken"]["type"] == "http"
    assert schemes["BearerToken"]["scheme"] == "bearer"


@pytest.mark.parametrize(("path", "verb"), BEARER_ENDPOINTS)
def test_de_projectendpoints_vragen_een_bearertoken(spec: dict[str, Any], path: str, verb: str) -> None:
    """Het geval uit de melding: hier stond APIKeyHeader, en dat gaf een onverklaarbare 401."""
    assert spec["paths"][path][verb]["security"] == [{"BearerToken": []}]


def test_de_rest_van_de_api_houdt_de_projectsleutel(spec: dict[str, Any]) -> None:
    """Het omgekeerde moet ook waar blijven, anders repareer je de ene helft stuk."""
    met_sleutel = [
        f"{verb} {path}"
        for path, methods in spec["paths"].items()
        if path.startswith("/api/")
        for verb, operation in methods.items()
        if isinstance(operation, dict) and operation.get("security") == [{"APIKeyHeader": []}]
    ]

    assert len(met_sleutel) > 50, "vrijwel de hele API hoort op de projectsleutel te zitten"
    assert "post /api/v2/projects" not in met_sleutel


def test_elke_bearerroute_staat_ook_zo_in_het_document(spec: dict[str, Any]) -> None:
    """De vergrendeling: geen lijst met paden, maar de routes zelf.

    Zet iemand ``@validate_user_token`` op een nieuwe route en vergeet hij de documentatie,
    dan faalt deze test in plaats van dat een gegenereerde client het later ontdekt.
    """
    missing: list[str] = []
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not getattr(endpoint, REQUIRES_USER_TOKEN, False):
            continue
        for verb in getattr(route, "methods", None) or []:
            operation = spec["paths"].get(route.path, {}).get(verb.lower())
            if operation and operation.get("security") != [{"BearerToken": []}]:
                missing.append(f"{verb} {route.path}")

    assert not missing, f"deze routes willen een bearertoken maar zeggen iets anders in het document: {missing}"
