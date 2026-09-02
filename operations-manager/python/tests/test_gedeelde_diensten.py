"""Tests voor de meetlaag onder /admin/diensten.

Waar deze tests op letten, in volgorde van belang:

1. **"kon niet meten" is niet hetzelfde als "niets te melden".** Dat verschil is de
   aanleiding voor de hele pagina: op het dashboard werd een mislukte meting op DEBUG
   gelogd, waardoor een kapotte grafiek er identiek uitzag als "geen verkeer". Hier moet
   een mislukte meting ``gemeten=False`` opleveren, een ``fout`` meegeven EN op WARNING
   in de log staan.
2. **Een query per BLOK, niet per rij.** Het projectdashboard doet 132 ArgoCD-aanroepen
   per weergave; dat schaalt lineair mee met het platform. Deze tests pinnen dat het
   aantal aanroepen niet met het aantal PVC's of databases meegroeit.
3. **De drempels zijn de drempels.** Alerting is de volgende stap en moet dezelfde
   grenzen gebruiken, dus de beoordeling loopt via ``DREMPELS`` en niet via losse
   getallen in de weergave.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.services.gedeelde_diensten import (
    _DATABASE_QUERIES,
    _OPSLAG_QUERIES,
    _RESOURCE_QUERIES,
    DREMPELS,
    ONGEMETEN_DIENSTEN,
    STATUS_KRITIEK,
    STATUS_OK,
    STATUS_ONBEKEND,
    STATUS_WAARSCHUWING,
    Blok,
    ResourceRij,
    beoordeel,
    haal_databases,
    haal_opslag,
    haal_resources,
    zwaarste,
)


def _reeks(labels: dict[str, str], waarde: str) -> dict[str, Any]:
    """Een instant-resultaat zoals beide connectoren het opleveren."""
    return {"metric": labels, "value": [1_787_000_000.0, waarde]}


def _pvc(namespace: str, claim: str, waarde: str) -> dict[str, Any]:
    return _reeks({"namespace": namespace, "persistentvolumeclaim": claim}, waarde)


def _db(namespace: str, pod: str, datname: str, waarde: str) -> dict[str, Any]:
    return _reeks({"namespace": namespace, "pod": pod, "datname": datname}, waarde)


class _Connector:
    """Een metriekconnector die per query een vast antwoord geeft.

    De sleutel is de QUERYTEKST zelf, opgezocht in ``_OPSLAG_QUERIES`` /
    ``_DATABASE_QUERIES``. Zo faalt een test zodra een query verandert zonder dat het
    antwoord meeverandert, in plaats van stilletjes een leeg blok te leveren.
    """

    def __init__(self, antwoorden: dict[str, list[dict[str, Any]]]) -> None:
        self.antwoorden = antwoorden
        self.queries: list[str] = []

    async def custom_query(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        naam = {v: k for k, v in {**_OPSLAG_QUERIES, **_DATABASE_QUERIES}.items()}[query]
        return self.antwoorden.get(naam, [])


def _met_connector(connector: Any) -> Any:
    return patch("opi.services.gedeelde_diensten.get_metrics_connector", AsyncMock(return_value=connector))


# ---------------------------------------------------------------------------
# Drempels
# ---------------------------------------------------------------------------


class TestDrempels:
    def test_onder_de_waarschuwing_is_ok(self) -> None:
        assert beoordeel("pvc_vulling", 10.0) == STATUS_OK

    def test_op_de_waarschuwing_valt_op(self) -> None:
        """Precies OP de grens telt al mee: een drempel is inclusief, anders glipt hij."""
        assert beoordeel("pvc_vulling", DREMPELS["pvc_vulling"].waarschuwing) == STATUS_WAARSCHUWING

    def test_op_de_kritieke_grens_is_kritiek(self) -> None:
        assert beoordeel("pvc_vulling", DREMPELS["pvc_vulling"].kritiek) == STATUS_KRITIEK

    def test_de_pvc_van_927_procent_is_kritiek(self) -> None:
        """Het getal uit de meting van 18 augustus 2026, dat niemand zag."""
        assert beoordeel("pvc_vulling", 92.7) == STATUS_KRITIEK

    def test_geen_waarde_is_onbekend_en_niet_ok(self) -> None:
        """Niet kunnen meten is geen goed nieuws en mag nooit als groen doorgaan."""
        assert beoordeel("pvc_vulling", None) == STATUS_ONBEKEND

    def test_zwaarste_kiest_de_ernstigste(self) -> None:
        assert zwaarste([STATUS_OK, STATUS_KRITIEK, STATUS_WAARSCHUWING]) == STATUS_KRITIEK
        assert zwaarste([STATUS_OK, STATUS_ONBEKEND]) == STATUS_OK
        assert zwaarste([]) == STATUS_OK

    def test_elke_drempel_heeft_een_eenheid_en_uitleg(self) -> None:
        """De pagina toont de drempels; zonder eenheid en uitleg is dat een rij getallen."""
        for drempel in DREMPELS.values():
            assert drempel.eenheid, drempel.naam
            assert drempel.uitleg, drempel.naam
            assert drempel.kritiek >= drempel.waarschuwing, drempel.naam


class TestOngemetenDiensten:
    def test_redis_en_minio_worden_benoemd(self) -> None:
        """Weglaten zou als 'in orde' lezen; ze horen op de pagina te STAAN."""
        namen = {dienst.naam for dienst in ONGEMETEN_DIENSTEN}
        assert namen == {"Redis", "MinIO"}

    def test_elke_ongemeten_dienst_zegt_wat_ervoor_nodig_is(self) -> None:
        for dienst in ONGEMETEN_DIENSTEN:
            assert dienst.reden
            assert dienst.nodig


# ---------------------------------------------------------------------------
# Opslag
# ---------------------------------------------------------------------------


class TestHaalOpslag:
    @pytest.mark.asyncio
    async def test_volst_staat_bovenaan(self) -> None:
        connector = _Connector(
            {
                "vulling": [
                    _pvc("rig-prd-operations", "minio-storage-versioned", "40.5"),
                    _pvc("rig-prd-ubbw-0i1", "production-typesense-data-pvc", "92.7"),
                    _pvc("rig-prd-algor-odc-infrastructure", "algor-odc-db-1", "60.8"),
                ],
                "capaciteit": [
                    _pvc("rig-prd-operations", "minio-storage-versioned", "1073741824"),
                    _pvc("rig-prd-ubbw-0i1", "production-typesense-data-pvc", "1073741824"),
                    _pvc("rig-prd-algor-odc-infrastructure", "algor-odc-db-1", "1073741824"),
                ],
            }
        )
        with _met_connector(connector):
            blok = await haal_opslag()

        assert blok.gemeten is True
        assert blok.fout is None
        assert [rij.claim for rij in blok.rijen] == [
            "production-typesense-data-pvc",
            "algor-odc-db-1",
            "minio-storage-versioned",
        ]
        assert blok.rijen[0].status == STATUS_KRITIEK

    @pytest.mark.asyncio
    async def test_volle_inodetabel_kleurt_de_rij_ook(self) -> None:
        """Een volle inodetabel geeft 'no space left' terwijl de bytes meevallen."""
        connector = _Connector(
            {
                "vulling": [_pvc("rig-prd-x", "data", "12.0")],
                "inodes": [_pvc("rig-prd-x", "data", "97.0")],
            }
        )
        with _met_connector(connector):
            blok = await haal_opslag()

        assert blok.rijen[0].vulling_procent == pytest.approx(12.0)
        assert blok.rijen[0].status == STATUS_KRITIEK

    @pytest.mark.asyncio
    async def test_een_pvc_zonder_meting_zakt_naar_onderen(self) -> None:
        connector = _Connector(
            {
                "vulling": [_pvc("rig-prd-x", "gemeten", "5.0")],
                "capaciteit": [
                    _pvc("rig-prd-x", "gemeten", "1073741824"),
                    _pvc("rig-prd-x", "ongemeten", "1073741824"),
                ],
            }
        )
        with _met_connector(connector):
            blok = await haal_opslag()

        assert [rij.claim for rij in blok.rijen] == ["gemeten", "ongemeten"]
        assert blok.rijen[1].vulling_procent is None
        assert blok.rijen[1].status == STATUS_ONBEKEND

    @pytest.mark.asyncio
    async def test_niets_gevonden_is_gemeten_en_leeg(self) -> None:
        """De sandbox heeft deze metrieken misschien niet; dat is heel, niet stuk."""
        with _met_connector(_Connector({})):
            blok = await haal_opslag()

        assert blok.gemeten is True
        assert blok.fout is None
        assert blok.rijen == []

    @pytest.mark.asyncio
    async def test_een_mislukte_meting_zegt_dat_hij_mislukte(self, caplog: pytest.LogCaptureFixture) -> None:
        """Niet stil, niet leeg: gemeten=False, een fout EN een regel op WARNING.

        Dit is de test die de bug van het dashboard pint. Zonder dit onderscheid ziet
        "de bron is onbereikbaar" er precies zo uit als "er is niets aan de hand".
        """
        connector = AsyncMock()
        connector.custom_query.side_effect = RuntimeError("prometheus onbereikbaar")

        with _met_connector(connector), caplog.at_level(logging.WARNING, logger="opi.services.gedeelde_diensten"):
            blok = await haal_opslag()

        assert blok.gemeten is False
        assert blok.rijen == []
        assert "prometheus onbereikbaar" in (blok.fout or "")
        assert [record.levelno for record in caplog.records] == [logging.WARNING]

    @pytest.mark.asyncio
    async def test_een_query_per_blok_en_niet_per_pvc(self) -> None:
        """Het aantal aanroepen mag niet met het aantal PVC's meegroeien."""
        veel = [_pvc("rig-prd-x", f"claim-{nummer}", "10.0") for nummer in range(50)]
        connector = _Connector({"vulling": veel})
        with _met_connector(connector):
            blok = await haal_opslag()

        assert len(blok.rijen) == 50
        assert len(connector.queries) == len(_OPSLAG_QUERIES)


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------


class TestHaalDatabases:
    @pytest.mark.asyncio
    async def test_grootte_verbindingen_en_wachtenden(self) -> None:
        connector = _Connector(
            {
                "grootte": [
                    _db("rig-system", "rig-db-1", "forgejo", "24049331"),
                    _db("rig-system", "rig-db-1", "keycloak", "19166899"),
                ],
                "verbindingen": [
                    _db("rig-system", "rig-db-1", "forgejo", "2"),
                    _db("rig-system", "rig-db-1", "keycloak", "3"),
                ],
                "wachtend": [_reeks({"namespace": "rig-system", "pod": "rig-db-1"}, "0")],
            }
        )
        with _met_connector(connector):
            blok = await haal_databases()

        assert blok.gemeten is True
        # Grootste eerst.
        assert [rij.database for rij in blok.rijen] == ["forgejo", "keycloak"]
        assert blok.rijen[0].grootte_bytes == pytest.approx(24049331)
        assert blok.rijen[0].verbindingen == pytest.approx(2)

        assert len(blok.extra_rijen) == 1
        instantie = blok.extra_rijen[0]
        assert instantie.instantie == "rig-db-1"
        # De verbindingen van de instantie zijn de SOM van zijn databases; wachtenden
        # bestaan alleen hier, want cnpg_backends_waiting_total heeft geen datname.
        assert instantie.verbindingen == pytest.approx(5)
        assert instantie.wachtend == pytest.approx(0)
        assert instantie.status == STATUS_OK

    @pytest.mark.asyncio
    async def test_wachtende_verbindingen_vallen_op(self) -> None:
        connector = _Connector(
            {
                "grootte": [_db("rig-system", "rig-db-1", "forgejo", "1")],
                "wachtend": [_reeks({"namespace": "rig-system", "pod": "rig-db-1"}, "7")],
            }
        )
        with _met_connector(connector):
            blok = await haal_databases()

        assert blok.extra_rijen[0].status == STATUS_KRITIEK

    @pytest.mark.asyncio
    async def test_een_hangende_transactie_kleurt_de_rij(self) -> None:
        """Een transactie die blijft hangen houdt vacuum tegen."""
        connector = _Connector(
            {
                "grootte": [_db("rig-system", "rig-db-1", "forgejo", "1")],
                "langste_transactie": [_db("rig-system", "rig-db-1", "forgejo", "7200")],
            }
        )
        with _met_connector(connector):
            blok = await haal_databases()

        assert blok.rijen[0].status == STATUS_KRITIEK

    @pytest.mark.asyncio
    async def test_een_mislukte_meting_zegt_dat_hij_mislukte(self, caplog: pytest.LogCaptureFixture) -> None:
        connector = AsyncMock()
        connector.custom_query.side_effect = RuntimeError("mimir weigert")

        with _met_connector(connector), caplog.at_level(logging.WARNING, logger="opi.services.gedeelde_diensten"):
            blok = await haal_databases()

        assert blok.gemeten is False
        assert "mimir weigert" in (blok.fout or "")
        assert [record.levelno for record in caplog.records] == [logging.WARNING]

    @pytest.mark.asyncio
    async def test_een_query_per_blok_en_niet_per_database(self) -> None:
        veel = [_db("rig-system", "rig-db-1", f"db-{nummer}", "1000") for nummer in range(40)]
        connector = _Connector({"grootte": veel})
        with _met_connector(connector):
            blok = await haal_databases()

        assert len(blok.rijen) == 40
        assert len(connector.queries) == len(_DATABASE_QUERIES)

    @pytest.mark.asyncio
    async def test_nan_telt_niet_als_meting(self) -> None:
        """Een deling zonder noemer levert NaN; dat is geen nul maar 'onbekend'."""
        connector = _Connector(
            {
                "grootte": [_db("rig-system", "rig-db-1", "forgejo", "1000")],
                "langste_transactie": [_db("rig-system", "rig-db-1", "forgejo", "NaN")],
            }
        )
        with _met_connector(connector):
            blok = await haal_databases()

        assert blok.rijen[0].langste_transactie_seconden is None


# ---------------------------------------------------------------------------
# Resourcegebruik
# ---------------------------------------------------------------------------


class _ResourceConnector:
    """Zoals ``_Connector``, maar voor de queries met een namespacefilter erin.

    De sleutel kan hier niet de kale querytekst zijn: die wordt pas op het moment van
    meten samengesteld uit de ingestelde namespaces. Er wordt daarom teruggezocht via het
    SJABLOON, zodat een gewijzigde query nog steeds een test breekt in plaats van
    stilzwijgend een leeg blok te leveren.
    """

    def __init__(self, antwoorden: dict[str, list[dict[str, Any]]], namespaces: list[str]) -> None:
        self.antwoorden = antwoorden
        self.queries: list[str] = []
        filter_regex = "|".join(namespaces)
        self.per_query = {
            sjabloon.format(namespaces=filter_regex): naam for naam, sjabloon in _RESOURCE_QUERIES.items()
        }

    async def custom_query(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return self.antwoorden.get(self.per_query[query], [])


def _ns(namespace: str, waarde: str) -> dict[str, Any]:
    return _reeks({"namespace": namespace}, waarde)


def _met_namespaces(namespaces: list[str]) -> Any:
    return patch("opi.core.cluster_config.get_service_namespaces", return_value=namespaces)


class TestHaalResources:
    @pytest.mark.asyncio
    async def test_gevraagd_staat_naast_gebruikt(self) -> None:
        """De hele reden voor dit blok: er wordt op de REQUEST gefactureerd, niet op gebruik."""
        namespaces = ["rig-prd-operations", "rig-prd-backup"]
        connector = _ResourceConnector(
            {
                "geheugen_gebruikt": [_ns("rig-prd-operations", "1073741824")],
                "geheugen_gevraagd": [_ns("rig-prd-operations", "3221225472")],
                "geheugen_limiet": [_ns("rig-prd-operations", "4294967296")],
            },
            namespaces,
        )
        with _met_namespaces(namespaces), _met_connector(connector):
            blok = await haal_resources()

        rij = blok.rijen[0]
        assert rij.geheugen_gebruikt == pytest.approx(1073741824)
        assert rij.geheugen_gevraagd == pytest.approx(3221225472)
        assert rij.geheugen_limiet == pytest.approx(4294967296)

    @pytest.mark.asyncio
    async def test_een_namespace_zonder_meting_blijft_staan(self) -> None:
        """Ingesteld maar niets gemeten is iets anders dan nooit ingesteld.

        De rijen komen daarom uit de CONFIGURATIE en niet uit het queryresultaat: een
        namespace die stilletjes uit de tabel valt is niet van een lege namespace te
        onderscheiden, en dat is precies de fout die je wilt zien.
        """
        namespaces = ["rig-prd-operations", "rig-prd-ron"]
        connector = _ResourceConnector({"cpu_gebruikt": [_ns("rig-prd-operations", "0.5")]}, namespaces)
        with _met_namespaces(namespaces), _met_connector(connector):
            blok = await haal_resources()

        assert [rij.namespace for rij in blok.rijen] == namespaces
        assert blok.rijen[1].cpu_gebruikt is None

    @pytest.mark.asyncio
    async def test_de_totaalrij_telt_op(self) -> None:
        namespaces = ["rig-prd-operations", "rig-prd-backup"]
        connector = _ResourceConnector(
            {
                "geheugen_gevraagd": [
                    _ns("rig-prd-operations", "1000"),
                    _ns("rig-prd-backup", "500"),
                ],
            },
            namespaces,
        )
        with _met_namespaces(namespaces), _met_connector(connector):
            blok = await haal_resources()

        (totaal,) = blok.extra_rijen
        assert totaal.geheugen_gevraagd == pytest.approx(1500)
        # Niets gemeten blijft None en wordt geen nul: een lege som is geen meting.
        assert totaal.cpu_limiet is None

    @pytest.mark.asyncio
    async def test_alleen_de_ingestelde_namespaces_worden_bevraagd(self) -> None:
        """Er mag geen projectnamespace in glippen; deze pagina gaat over ONZE diensten."""
        namespaces = ["rig-prd-operations", "rig-prd-backup"]
        connector = _ResourceConnector({}, namespaces)
        with _met_namespaces(namespaces), _met_connector(connector):
            await haal_resources()

        assert len(connector.queries) == len(_RESOURCE_QUERIES)
        for query in connector.queries:
            assert 'namespace=~"rig-prd-operations|rig-prd-backup"' in query

    @pytest.mark.asyncio
    async def test_zonder_ingestelde_namespaces_wordt_er_niet_gemeten(self) -> None:
        """Geen configuratie is geen "alles": dan zou de pagina projecten gaan tonen."""
        connector = _ResourceConnector({}, [])
        with _met_namespaces([]), _met_connector(connector):
            blok = await haal_resources()

        assert blok.gemeten is False
        assert "service_namespaces" in (blok.fout or "")
        assert connector.queries == []

    @pytest.mark.asyncio
    async def test_een_mislukte_meting_zegt_dat_hij_mislukte(self, caplog: pytest.LogCaptureFixture) -> None:
        connector = AsyncMock()
        connector.custom_query.side_effect = RuntimeError("mimir onbereikbaar")

        with (
            _met_namespaces(["rig-prd-operations"]),
            _met_connector(connector),
            caplog.at_level(logging.WARNING, logger="opi.services.gedeelde_diensten"),
        ):
            blok = await haal_resources()

        assert blok.gemeten is False
        assert "mimir onbereikbaar" in (blok.fout or "")
        assert [record.levelno for record in caplog.records] == [logging.WARNING]


class TestResourceSjabloon:
    """Dat het resourceblok ook RENDERT, in alle drie zijn toestanden.

    De meetlaag hierboven kan kloppen terwijl het scherm leeg blijft: een componenttag met
    een attribuut dat niet bestaat breekt pas bij het renderen. Deze drie gevallen zijn de
    drie uitkomsten die de route kan opleveren, en het derde - "kon niet meten" - is
    degene die op het scherm te zien moet zijn en niet in een log.
    """

    SJABLOON = "bg/_gedeelde-diensten-resources.html.j2"

    def _render(self, blok: Any) -> str:
        from opi.core.templates_lotc import templates_lotc

        html = templates_lotc.env.get_template(self.SJABLOON).render({"blok": blok})
        assert "<c-" not in html, "onvervangen componenttag"
        return html

    def test_de_drie_getallen_staan_op_het_scherm(self) -> None:
        rij = ResourceRij(
            namespace="rig-prd-operations",
            cpu_gebruikt=0.464,
            cpu_gevraagd=2.0,
            cpu_limiet=8.0,
            geheugen_gebruikt=27_379_029_474.0,
            geheugen_gevraagd=46_285_265_305.0,
            geheugen_limiet=76_890_000_000.0,
            opslag_gebruikt=1_073_741_824.0,
            opslag_capaciteit=10_737_418_240.0,
        )
        html = self._render(Blok(gemeten=True, rijen=[rij], extra_rijen=[]))

        assert "rig-prd-operations" in html
        # Gevraagd is de reden voor dit blok; als die kolom wegvalt is het blok zinloos.
        assert "43.1 GiB" in html
        assert "25.5 GiB" in html
        assert "71.6 GiB" in html

    def test_een_namespace_zonder_meting_krijgt_streepjes(self) -> None:
        leeg = ResourceRij("rig-prd-ron", None, None, None, None, None, None, None, None)
        html = self._render(Blok(gemeten=True, rijen=[leeg], extra_rijen=[]))

        assert "rig-prd-ron" in html

    def test_kon_niet_meten_staat_op_het_scherm(self) -> None:
        html = self._render(Blok(gemeten=False, fout="mimir onbereikbaar"))

        assert "Kon niet meten" in html
        assert "mimir onbereikbaar" in html
