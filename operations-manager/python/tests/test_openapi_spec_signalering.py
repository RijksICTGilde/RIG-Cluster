"""Een client kan zien dat de spec veranderd is, zonder hem elke keer te downloaden.

DE MELDING (zad-cli, punt 1)

De CLI leest ``/openapi.json`` live in plaats van uit een meegeleverde kopie, want daar
staat in wat een veld accepteert. Het document gaf alleen geen enkel signaal dat het
veranderd was: ``info.version`` stond op ``0.1.0`` en bleef daar een week lang staan, en er
kwam geen ``ETag`` of ``Last-Modified`` mee. Een client kan dan alleen op TIJD cachen, en die
cache stond op een uur.

Wat dat kostte, is gemeten: op de dag dat de standaard van ``sleep-mode.wake-mode``
veranderde, vertelde de CLI zijn gebruikers een uur lang de oude waarheid.

TWEE ANTWOORDEN, WANT ZE DOEN IETS ANDERS

``x-spec-revision`` in ``info`` zegt WELKE build dit document maakte; dat lees je zonder een
tweede verzoek. De ``ETag`` maakt een CONDITIONELE GET mogelijk: met ``If-None-Match`` krijg
je een 304 zonder body als er niets veranderde, en dan kan de cache op verandering in plaats
van op tijd.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.middleware.openapi_etag import OPENAPI_PATH, _etag_for
from opi.server import app


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app.openapi()


class TestWelkeBuildDitDocumentMaakte:
    def test_de_spec_noemt_zijn_herkomst(self, spec: dict[str, Any]) -> None:
        revisie = spec["info"]["x-spec-revision"]

        assert set(revisie) == {"commit", "branch", "build_date"}

    def test_de_herkomst_komt_uit_dezelfde_bron_als_version(self, spec: dict[str, Any]) -> None:
        """Anders ontstaat er een tweede waarheid over welke build er draait."""
        from opi.core.version import get_version_info

        info = get_version_info()

        assert spec["info"]["x-spec-revision"]["commit"] == info["commit"]


class TestDeEtag:
    def test_hetzelfde_document_geeft_dezelfde_etag(self, spec: dict[str, Any]) -> None:
        assert _etag_for(spec)[0] == _etag_for(spec)[0]

    def test_de_volgorde_van_sleutels_verandert_de_etag_niet(self) -> None:
        """Een herstart mag geen nieuwe ETag opleveren.

        Een dict heeft geen betekenisvolle volgorde, maar zijn JSON-vorm wel: zonder
        sorteren zou elke herstart met een andere invoegvolgorde elke client onnodig laten
        downloaden.
        """
        eerst = {"a": 1, "b": {"x": 1, "y": 2}}
        andersom = {"b": {"y": 2, "x": 1}, "a": 1}

        assert _etag_for(eerst)[0] == _etag_for(andersom)[0]

    def test_een_wijziging_geeft_een_andere_etag(self, spec: dict[str, Any]) -> None:
        gewijzigd = {**spec, "info": {**spec["info"], "title": "iets anders"}}

        assert _etag_for(spec)[0] != _etag_for(gewijzigd)[0]

    def test_de_middleware_bemoeit_zich_alleen_met_het_document(self) -> None:
        assert OPENAPI_PATH == "/openapi.json"


class TestHetEndpointDatDeSpecAanwijst:
    """Punt 9: ``base-domain`` verwees naar een endpoint dat niet bestond."""

    def test_de_bron_van_base_domain_wijst_naar_een_bestaande_route(self, spec: dict[str, Any]) -> None:
        veld = spec["components"]["schemas"]["PublishOnWebDeploymentConfig"]["properties"]["base-domain"]
        bron = veld["x-choices-source"]
        methode, pad = bron["endpoint"].split(" ", 1)

        assert pad in spec["paths"], f"{pad} staat niet in het document"
        assert methode.lower() in spec["paths"][pad]

    def test_het_endpoint_levert_het_pad_dat_de_bron_belooft(self, spec: dict[str, Any]) -> None:
        """De verwijzing is pas iets waard als je op die plek ook echt waarden vindt."""
        from opi.api.v2.models import ClusterListResponse

        velden = ClusterListResponse.model_json_schema(by_alias=True)["$defs"]["ClusterInfo"]["properties"]

        assert "base-domains" in velden
