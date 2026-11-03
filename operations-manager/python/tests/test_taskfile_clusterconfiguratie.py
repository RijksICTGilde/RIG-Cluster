"""Welke clusterconfiguratie Task laadt: met CLUSTER_TYPE alleen die van dat cluster.

Op 3 november 2026 liep een deploy voor productie met CLUSTER_TYPE=odcn-production terwijl
staging geselecteerd was. Het prod-bestand zet geen SECRETS_DIR, de selectie wel, en die
vulde hem aan: de deploy schreef het prod-env-secret in de werkmap van zad-staging-secrets.

De dotenv-regel uit de echte Taskfile wordt hier gedraaid, met nepbestanden per cluster.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from tests.programma import echt_programma

REPO_ROOT = Path(__file__).resolve().parents[3]


def _laad(tmp_path: Path, cluster_type: str | None) -> str:
    dotenv = yaml.safe_load((REPO_ROOT / "Taskfile.yaml").read_text())["dotenv"]
    (tmp_path / ".env").write_text("")
    (tmp_path / ".env-taskfile-odcn-production").write_text("CLUSTER_TYPE=odcn-production\nRIG_NAMESPACE=rig-prd\n")
    (tmp_path / ".env-taskfile-odcn-staging").write_text(
        "CLUSTER_TYPE=odcn-staging\nRIG_NAMESPACE=rig-staging\nSECRETS_DIR=../zad-staging-secrets\n"
    )
    # De selectie staat op staging.
    (tmp_path / ".env-taskfile-current").write_text((tmp_path / ".env-taskfile-odcn-staging").read_text())
    (tmp_path / "Taskfile.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "3",
                "dotenv": dotenv,
                "tasks": {"toon": {"silent": True, "cmds": ["echo {{.RIG_NAMESPACE}}:{{.SECRETS_DIR}}"]}},
            }
        )
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if cluster_type:
        env["CLUSTER_TYPE"] = cluster_type
    proc = subprocess.run(
        [echt_programma("task"), "toon"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_met_cluster_type_vult_de_selectie_niets_aan(tmp_path: Path) -> None:
    assert _laad(tmp_path, "odcn-production") == "rig-prd:"


def test_met_cluster_type_van_de_selectie_komt_alles_uit_dat_bestand(tmp_path: Path) -> None:
    assert _laad(tmp_path, "odcn-staging") == "rig-staging:../zad-staging-secrets"


def test_zonder_cluster_type_geldt_de_selectie(tmp_path: Path) -> None:
    assert _laad(tmp_path, None) == "rig-staging:../zad-staging-secrets"


@pytest.fixture(autouse=True)
def _task_aanwezig() -> None:
    echt_programma("task")
