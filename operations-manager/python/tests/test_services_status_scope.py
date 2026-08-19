"""Services status gaat over ONZE diensten, niet over wat gebruikers draaien.

De queries in ``gedeelde_diensten`` hebben geen namespacefilter: ze tellen op wat er is,
in een set per blok. Dat is met opzet zo, want een filter in PromQL zou een lijst
namespaces in elke query bakken en die loopt weg zodra er een project bij komt.

Het gevolg was wel dat elke project-PVC en elke projectdatabase op de beheerpagina
verscheen. Er wordt daarom afgetrokken in plaats van gefilterd: de namespaces van de
projecten in de store vallen af, en wat overblijft is van ons.

Let op de RICHTING. Dit is een uitsluiting en geen lijst van onze eigen namespaces. Zo'n
lijst zou drijven: komt er een infrastructuuronderdeel bij, dan valt het stil buiten beeld
en ziet niemand dat. Andersom komt een onbekende namespace juist IN beeld, en dat merk je.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.services import gedeelde_diensten


class _Project:
    def __init__(self, naam: str) -> None:
        self.name = naam


class _Store:
    def __init__(self, namen: list[str]) -> None:
        self.namen = namen

    def get_all(self) -> list[_Project]:
        return [_Project(n) for n in self.namen]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: _Store(["wies", "toets-hn7"]))
    monkeypatch.setattr("opi.core.cluster_config.get_namespace_prefix", lambda _cluster: "rig-prd-")


def test_de_projectnamespaces_worden_afgeleid(store: None) -> None:
    gevonden = gedeelde_diensten.projectnamespaces()

    assert "rig-prd-wies" in gevonden
    assert "rig-prd-toets-hn7" in gevonden
    # De infrastructuurnamespace van een project is ook van de gebruiker.
    assert "rig-prd-wies-infrastructure" in gevonden


def test_onze_eigen_namespaces_vallen_er_niet_onder(store: None) -> None:
    gevonden = gedeelde_diensten.projectnamespaces()

    assert "rig-prd-operations" not in gevonden
    assert "argocd" not in gevonden


def test_een_onbekend_cluster_sluit_niets_uit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liever alles tonen dan stilletjes alles wegfilteren."""

    def _weiger(_cluster: str) -> str:
        raise ValueError("onbekend cluster")

    monkeypatch.setattr("opi.core.cluster_config.get_namespace_prefix", _weiger)

    assert gedeelde_diensten.projectnamespaces() == set()


@pytest.mark.asyncio
async def test_een_project_pvc_staat_niet_op_de_pagina(monkeypatch: pytest.MonkeyPatch, store: None) -> None:
    """Dit is de melding zelf: de PVC's van projecten hoeven er niet in."""

    async def _antwoorden(_queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        reeks = [
            {"metric": {"namespace": "rig-prd-operations", "persistentvolumeclaim": "opi-data"}, "value": [0, "50"]},
            {"metric": {"namespace": "rig-prd-wies", "persistentvolumeclaim": "uploads"}, "value": [0, "90"]},
        ]
        return {"vulling": reeks, "gebruikt": reeks, "capaciteit": reeks, "inodes": reeks}

    monkeypatch.setattr(gedeelde_diensten, "_voer_queries_uit", _antwoorden)

    blok = await gedeelde_diensten.haal_opslag()

    namespaces = [rij.namespace for rij in blok.rijen]
    assert "rig-prd-operations" in namespaces
    assert "rig-prd-wies" not in namespaces, "een project-PVC staat nog op de beheerpagina"


@pytest.mark.asyncio
async def test_een_projectdatabase_staat_niet_op_de_pagina(monkeypatch: pytest.MonkeyPatch, store: None) -> None:
    async def _antwoorden(_queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        reeks = [
            {"metric": {"namespace": "rig-prd-operations", "pod": "rig-db-1", "datname": "opi"}, "value": [0, "10"]},
            {"metric": {"namespace": "rig-prd-wies", "pod": "wies-db-1", "datname": "wies"}, "value": [0, "20"]},
        ]
        instanties = [
            {"metric": {"namespace": "rig-prd-operations", "pod": "rig-db-1"}, "value": [0, "0"]},
            {"metric": {"namespace": "rig-prd-wies", "pod": "wies-db-1"}, "value": [0, "3"]},
        ]
        return {
            "grootte": reeks,
            "verbindingen": reeks,
            "langste_transactie": reeks,
            "xid_leeftijd": reeks,
            "wachtend": instanties,
        }

    monkeypatch.setattr(gedeelde_diensten, "_voer_queries_uit", _antwoorden)

    blok = await gedeelde_diensten.haal_databases()

    assert [rij.namespace for rij in blok.rijen] == ["rig-prd-operations"]
    assert [rij.namespace for rij in blok.extra_rijen] == ["rig-prd-operations"]


def test_geen_enkele_query_telt_dezelfde_reeks_dubbel() -> None:
    """Dezelfde target wordt door twee jobs gescrapet; optellen verdubbelt de waarde.

    ``cnpg_pg_database_size_bytes`` komt zowel uit de job 'cloudnative-pg' als uit
    'kubernetes-pods'. Dat viel niet op zolang er maar een van de twee binnen het
    staleness-venster viel: de som telde dan een enkele reeks op. Zodra ``last_over_time``
    ze allebei zichtbaar maakt, telt ``sum by`` ze bij elkaar op. Gemeten op de sandbox:
    de keycloak-database staat op 92 MB, de som maakte er 183 MB van.

    Per (namespace, pod, datname) bestaat er logisch EEN reeks, en per (namespace, pvc)
    ook. Er valt dus niets op te tellen.
    """
    from opi.services.gedeelde_diensten import _DATABASE_QUERIES, _OPSLAG_QUERIES

    for naam, query in {**_OPSLAG_QUERIES, **_DATABASE_QUERIES}.items():
        assert "sum by" not in query, (
            f"query '{naam}' telt op over reeksen die door meerdere jobs gescrapet worden "
            f"en verdubbelt daarmee de waarde; gebruik max by: {query}"
        )


def test_elke_meting_kijkt_terug_over_het_staleness_venster_heen() -> None:
    """Een deel van deze reeksen komt uit een job die elke twee uur scrapet."""
    from opi.services.gedeelde_diensten import _DATABASE_QUERIES, _OPSLAG_QUERIES

    for naam, query in {**_OPSLAG_QUERIES, **_DATABASE_QUERIES}.items():
        assert "last_over_time" in query, (
            f"query '{naam}' is een kale instant-query en mist daarmee alles wat langer dan "
            f"vijf minuten geleden gescrapet is: {query}"
        )
