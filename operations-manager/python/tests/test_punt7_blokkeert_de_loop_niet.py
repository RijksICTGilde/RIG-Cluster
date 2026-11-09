"""De overgebleven synchrone stappen houden de event loop niet meer vast.

Een verzameling kleine stappen uit RC-263 punt 7: een chisel-tunnel die tot 30 s op een
socket pollt, de tijdreeksen van Prometheus die op ``future.result()`` wachtten binnen een
``async def``, en de CA-hash die per component ``openssl`` startte.

Net als de andere toetsen in deze reeks meten ze gedrag: er loopt een meeloper mee die per
tick de klok leest, en het grootste gat tussen twee ticks is wat de loop stilstond.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from opi.connectors.prometheus import PrometheusConnector
from opi.core.cluster_config import _ca_hash_cache, _compute_ca_hash
from opi.services.metrics_queries import TimeWindow, deployment_pod_prefixes, timeseries_per_workload
from opi.utils.chisel_helper import chisel_tunnel
from tests.loop_meeloper import meeloper

if TYPE_CHECKING:
    from pathlib import Path

_TRAAG = 0.3
_GRENS = 0.1


@pytest.mark.asyncio
async def test_het_opzetten_van_een_chisel_tunnel_blokkeert_de_loop_niet() -> None:
    """``start_tunnel`` pollt met ``time.sleep`` op een synchrone socket-connect."""
    verbinding = MagicMock()

    def traag_starten(**_kwargs: object) -> None:
        time.sleep(_TRAAG)

    verbinding.start_tunnel = traag_starten
    verbinding.stop_tunnel = lambda: time.sleep(_TRAAG)
    verbinding.get_local_endpoint = MagicMock(return_value={"host": "localhost", "port": 15432})

    config = {"server-url": "https://chisel.example.org", "username": "u", "password": "plain:w"}

    with patch("opi.utils.chisel_helper.ChiselConnector", MagicMock(return_value=verbinding)):
        async with meeloper() as meting:
            async with chisel_tunnel(config, "postgres.svc", 5432) as endpoint:
                assert endpoint["port"] == 15432

    meting.eis_onder(_GRENS, "het opzetten en afbreken van een chisel-tunnel")


@pytest.mark.asyncio
async def test_de_tijdreeksen_van_drie_componenten_blokkeren_de_loop_niet() -> None:
    """Negen queries per component liepen op ``future.result()`` binnen een async def.

    Meet de echte sandbox-backend: alleen de synchrone library-client is gefaket, want die
    is wat de loop vasthield.
    """
    verbinding = PrometheusConnector.__new__(PrometheusConnector)
    verbinding._initialized = True
    verbinding._prometheus_url = "http://prometheus:9090"
    verbinding.prom = MagicMock()
    verbinding.prom.custom_query_range.side_effect = lambda **_: time.sleep(_TRAAG) or []
    verbinding.prom.custom_query.side_effect = lambda *_a, **_k: time.sleep(_TRAAG) or []
    PrometheusConnector.is_connected = True

    async with meeloper() as meting:
        resultaat = await timeseries_per_workload(
            verbinding,
            "rig-demo",
            deployment_pod_prefixes("productie", ["backend", "frontend", "worker"]),
            TimeWindow(),
        )

    assert sorted(resultaat) == ["backend", "frontend", "worker"]
    meting.eis_onder(_GRENS, "de tijdreeksen van drie componenten")


def test_de_ca_hash_start_openssl_maar_een_keer(tmp_path: Path) -> None:
    """Het certificaat verandert niet tijdens de looptijd; openssl per component niet nodig."""
    certificaat = tmp_path / "ca.crt"
    certificaat.write_text("-----BEGIN CERTIFICATE-----\n")
    _ca_hash_cache.clear()

    antwoord = MagicMock(stdout="abcd1234\n", returncode=0)
    with patch("opi.core.cluster_config.subprocess.run", return_value=antwoord) as openssl:
        eerste = _compute_ca_hash(str(certificaat))
        tweede = _compute_ca_hash(str(certificaat))

    assert eerste == tweede == "abcd1234"
    assert openssl.call_count == 1


def test_een_lege_uitvoer_gaat_niet_in_de_cache(tmp_path: Path) -> None:
    """Een hash die er niet is hoort niet bewaard te blijven als antwoord."""
    certificaat = tmp_path / "ca.crt"
    certificaat.write_text("-----BEGIN CERTIFICATE-----\n")
    _ca_hash_cache.clear()

    with patch("opi.core.cluster_config.subprocess.run", return_value=MagicMock(stdout="\n", returncode=0)):
        assert _compute_ca_hash(str(certificaat)) == ""

    assert str(certificaat) not in _ca_hash_cache
