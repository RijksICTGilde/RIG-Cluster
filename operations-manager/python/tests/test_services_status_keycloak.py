"""Keycloak op de Services status-pagina, en de bron waar die vandaan komt.

De cijfers die hier getoond worden bestonden al: onze eigen Keycloak-extensie geeft ze
uit op /realms/master/rig-metrics (features/keycloak-rig-metrics.md) en de scrape-job
``keycloak-rig-metrics`` haalt ze op. Ze stonden alleen niet op de beheerpagina, en
Keycloak stond ook niet bij "wat er niet gemeten wordt": stilzwijgend overgeslagen.

DE BRON IS EEN ANDERE DAN DIE VAN DE ANDERE BLOKKEN, en dat is de kern. De kubelet- en
CNPG-cijfers komen op productie uit Mimir via de Grafana-connector. Deze metrieken zitten
daar NIET in; ze staan in onze eigen Prometheus. Vandaar PrometheusConnector en niet
get_metrics_connector. De test hieronder legt dat vast, want als iemand dit blok "netjes
gelijktrekt" met de rest, levert het stilletjes lege tabellen op.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.services import gedeelde_diensten


class _Prom:
    """Onze eigen Prometheus, met de reeksen die de extensie zou opleveren."""

    def __init__(self) -> None:
        self.gevraagd: list[str] = []

    async def custom_query(self, query: str) -> list[dict[str, Any]]:
        self.gevraagd.append(query)
        if "realms_total" in query:
            return [{"metric": {}, "value": [0, "12"]}]
        if "users_by_idp" in query:
            return [
                {"metric": {"realm": "wies", "idp_type": "saml"}, "value": [0, "40"]},
                {"metric": {"realm": "wies", "idp_type": "local"}, "value": [0, "2"]},
            ]
        if "users_total" in query:
            return [
                {"metric": {"realm": "wies"}, "value": [0, "42"]},
                {"metric": {"realm": "toets-hn7"}, "value": [0, "3"]},
            ]
        if "login_errors_total" in query:
            return [{"metric": {"realm": "wies"}, "value": [0, "5"]}]
        if "logins_total" in query:
            return [{"metric": {"realm": "wies"}, "value": [0, "130"]}]
        return []


@pytest.fixture
def prom(monkeypatch: pytest.MonkeyPatch) -> _Prom:
    verbinding = _Prom()
    monkeypatch.setattr("opi.connectors.prometheus.PrometheusConnector", lambda *a, **k: verbinding)
    return verbinding


@pytest.mark.asyncio
async def test_de_realms_komen_met_hun_gebruikers_en_logins(prom: _Prom) -> None:
    blok = await gedeelde_diensten.haal_keycloak()

    assert blok.gemeten
    per_realm = {rij.realm: rij for rij in blok.rijen}
    assert per_realm["wies"].gebruikers == 42.0
    assert per_realm["wies"].logins_24u == 130.0
    assert per_realm["wies"].mislukte_logins_24u == 5.0
    assert per_realm["wies"].gebruikers_per_idp == {"saml": 40.0, "local": 2.0}


@pytest.mark.asyncio
async def test_het_aantal_realms_staat_los_van_de_rijen(prom: _Prom) -> None:
    """Een realm zonder gebruikers levert geen rij op, maar bestaat wel."""
    blok = await gedeelde_diensten.haal_keycloak()

    assert blok.totaal == 12.0
    assert len(blok.rijen) == 2


@pytest.mark.asyncio
async def test_de_meeste_gebruikers_staan_bovenaan(prom: _Prom) -> None:
    blok = await gedeelde_diensten.haal_keycloak()

    assert [rij.realm for rij in blok.rijen] == ["wies", "toets-hn7"]


@pytest.mark.asyncio
async def test_de_logins_gaan_over_24_uur(prom: _Prom) -> None:
    """De scrape-job draait elke twee uur; een korter venster geeft lege cellen."""
    await gedeelde_diensten.haal_keycloak()

    login_queries = [q for q in prom.gevraagd if "rig_keycloak_logins_total" in q]
    assert login_queries, "er is geen loginquery gedaan"
    assert "[24h]" in login_queries[0], f"venster is niet 24h: {login_queries[0]}"


@pytest.mark.asyncio
async def test_dit_blok_gebruikt_onze_eigen_prometheus(monkeypatch: pytest.MonkeyPatch, prom: _Prom) -> None:
    """De grendel: via get_metrics_connector zijn deze metrieken er op productie niet.

    Mimir kent ze niet, dus wie dit blok gelijktrekt met de rest krijgt lege tabellen
    zonder foutmelding.
    """

    async def _verboden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("haal_keycloak mag niet via get_metrics_connector lopen")

    monkeypatch.setattr("opi.connectors.prometheus.get_metrics_connector", _verboden)

    blok = await gedeelde_diensten.haal_keycloak()

    assert blok.gemeten


@pytest.mark.asyncio
async def test_een_kapotte_bron_kost_het_blok_en_niet_de_pagina(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stuk:
        async def custom_query(self, query: str) -> list[dict[str, Any]]:
            raise RuntimeError("prometheus onbereikbaar")

    monkeypatch.setattr("opi.connectors.prometheus.PrometheusConnector", lambda *a, **k: _Stuk())

    blok = await gedeelde_diensten.haal_keycloak()

    assert blok.gemeten is False
    assert blok.fout is not None
    assert blok.rijen == []
