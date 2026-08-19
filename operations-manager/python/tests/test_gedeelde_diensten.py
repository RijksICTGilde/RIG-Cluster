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
    DREMPELS,
    ONGEMETEN_DIENSTEN,
    STATUS_KRITIEK,
    STATUS_OK,
    STATUS_ONBEKEND,
    STATUS_WAARSCHUWING,
    beoordeel,
    haal_databases,
    haal_opslag,
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
