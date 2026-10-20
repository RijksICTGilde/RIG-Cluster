"""Geen geheim op een commandoregel, en geen commandoregel in een metriek.

Een commandoregel is leesbaar voor elk proces in de container (``/proc/<pid>/cmdline``,
``ps``), en een labelwaarde van een metriek komt via Prometheus in Grafana terecht. BIO2
8.15 en 8.24 vragen dat beide geen gegevens bevatten die de beveiliging raken.

De guard onderaan bewaakt het terugkomen van de vlaggen: kopia, mc en chisel lezen
dezelfde waarden ook uit de omgeving, dus er is geen reden ze als argument te geven.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.chisel_connector import ChiselConnector
from opi.connectors.kopia import KopiaConnector, KopiaRepositoryConfig
from opi.connectors.minio_mc import MinioConnector
from opi.core.metrics import OPICollector

if TYPE_CHECKING:
    from prometheus_client.core import Metric

CONNECTORS = Path(__file__).resolve().parent.parent / "opi" / "connectors"
VERBODEN_VLAGGEN = ("--password", "--secret-access-key", "--access-key", "--auth")
VLAG_IN_ARGUMENT = re.compile('"({})"'.format("|".join(VERBODEN_VLAGGEN)))


def _familie(naam: str) -> Metric:
    return {familie.name: familie for familie in OPICollector().collect()}[naam]


def test_de_metriek_noemt_de_procesnaam_en_niet_de_commandoregel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Een nep-/proc met een kindproces van de eigen pid."""
    eigen_pid = os.getpid()
    kind = tmp_path / "4242"
    kind.mkdir()
    velden = ["0"] * 50
    velden[0] = "S"  # state, het eerste veld na comm
    velden[1] = str(eigen_pid)  # ppid
    velden[21] = "100"  # rss in pages
    (kind / "stat").write_text(f"4242 (kopia) {' '.join(velden)}\n")
    (kind / "cmdline").write_text("kopia\0repository\0connect\0--password\0geheim\0")
    monkeypatch.setattr("opi.core.metrics._PROC", str(tmp_path))

    familie = _familie("opi_child_process_rss_bytes")
    labels = {sample.labels["comm"] for sample in familie.samples}

    assert labels == {"kopia"}
    assert all("geheim" not in waarde for sample in familie.samples for waarde in sample.labels.values())


@pytest.mark.asyncio
async def test_kopia_krijgt_zijn_geheimen_via_de_omgeving() -> None:
    config = KopiaRepositoryConfig(
        s3_endpoint="minio:9000",
        s3_bucket="backups",
        s3_access_key="de-access-key",
        s3_secret_key="de-secret-key",
        s3_prefix="project",
        password="het-wachtwoord",
    )
    geheimen = {"de-access-key", "de-secret-key", "het-wachtwoord"}

    connector = KopiaConnector()
    with (
        patch.object(KopiaConnector, "is_kopia_available", True),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as exec_mock,
    ):
        exec_mock.return_value.communicate = AsyncMock(return_value=(b"[]", b""))
        exec_mock.return_value.returncode = 0
        await connector.list_snapshots(config)

    for aanroep in exec_mock.call_args_list:
        assert geheimen.isdisjoint(aanroep.args), aanroep.args
    verbind = next(a for a in exec_mock.call_args_list if "connect" in a.args)
    assert geheimen <= set(verbind.kwargs["env"].values())


@pytest.mark.asyncio
async def test_mc_krijgt_zijn_sleutels_via_de_omgeving_en_stdin() -> None:
    connector = MinioConnector()
    with (
        patch.object(MinioConnector, "is_mc_available", True),
        patch.object(MinioConnector, "_run_mc_command", new_callable=AsyncMock) as run_mock,
    ):
        run_mock.return_value = ("", "", 0)
        await connector.configure_alias("alias-x", "minio:9000", "de-access-key", "de-secret-key")
        await connector.create_user("alias-x", "gebruiker", "de-secret-key")

    for aanroep in run_mock.call_args_list:
        assert "de-secret-key" not in aanroep.args[0]
        assert "de-access-key" not in aanroep.args[0]
    assert connector.env["MC_HOST_alias-x"] == "http://de-access-key:de-secret-key@minio:9000"
    assert run_mock.call_args.kwargs["stdin_input"] == "de-secret-key\n"


def test_chisel_geeft_auth_niet_als_argument() -> None:
    connector = ChiselConnector(server_url="https://chisel", username="gebruiker", password="geheim")
    with (
        patch("opi.connectors.chisel_connector.subprocess.Popen") as popen,
        patch.object(ChiselConnector, "_wait_for_port", return_value=True),
    ):
        connector.start_tunnel("postgres", 5432, local_port=15432)

    cmd = popen.call_args.args[0]
    assert "--auth" not in cmd
    assert "gebruiker:geheim" not in cmd
    assert popen.call_args.kwargs["env"]["AUTH"] == "gebruiker:geheim"


def test_geen_geheimvlag_in_een_argumentenlijst_van_een_connector() -> None:
    """De guard: een van deze vlaggen in een lijst die naar een subproces gaat."""
    gevonden = []
    for bestand in sorted(CONNECTORS.glob("*.py")):
        for nummer, regel in enumerate(bestand.read_text().splitlines(), 1):
            if VLAG_IN_ARGUMENT.search(regel):
                gevonden.append(f"{bestand.name}:{nummer}: {regel.strip()}")
    assert not gevonden, "geheim op een commandoregel:\n" + "\n".join(gevonden)
