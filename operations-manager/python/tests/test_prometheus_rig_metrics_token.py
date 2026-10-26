"""Elke rig-metrics-scrape-job draagt het scrape-token, in elke Prometheus-overlay (RC-320).

Het Keycloak-endpoint ``/realms/master/rig-metrics`` weigert zonder Bearer-token (401).
De grendel rendert elke overlay, zodat ook een gekopieerde overlay niet zonder token uitgaat.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAYS = _REPO_ROOT / "infrastructure/bootstrap/infrastructure/prometheus/controller/overlays"

#: Het blok zoals kubernetes-pods het ook draagt: het token uit het secret
#: prometheus-metrics-auth, gemount onder /etc/prometheus/secrets/metrics-auth.
_TOKEN_BLOK = {
    "type": "Bearer",
    "credentials_file": "/etc/prometheus/secrets/metrics-auth/token",
}

#: Endpoints die zonder het scrape-token geen inhoud geven.
_TOKEN_AFGESCHERMD = {"/realms/master/rig-metrics"}


def _scrape_configs(overlay: Path) -> list[dict]:
    rendered = subprocess.run(
        ["kustomize", "build", str(overlay), "--load-restrictor", "LoadRestrictionsNone"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    configmap = next(
        document
        for document in yaml.safe_load_all(rendered)
        if document and document["kind"] == "ConfigMap" and document["metadata"]["name"] == "prometheus-config"
    )
    return yaml.safe_load(configmap["data"]["prometheus.yml"])["scrape_configs"]


@pytest.fixture(scope="module")
def jobs_per_overlay() -> dict[str, list[dict]]:
    """De scrape-jobs per overlay, op de gerenderde configmap: een patch die de hele
    configmap vervangt (zoals odcn-staging) toetst je niet door het patchbestand te lezen."""
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    return {
        overlay.name: _scrape_configs(overlay)
        for overlay in sorted(_OVERLAYS.iterdir())
        if (overlay / "kustomization.yaml").exists()
    }


def test_elke_rig_metrics_job_draagt_het_scrape_token(jobs_per_overlay: dict[str, list[dict]]) -> None:
    for overlay, jobs in jobs_per_overlay.items():
        for job in jobs:
            if job.get("metrics_path") in _TOKEN_AFGESCHERMD:
                assert job.get("authorization") == _TOKEN_BLOK, (
                    f"{overlay}: job {job['job_name']} scrapet {job['metrics_path']} zonder het scrape-token"
                )


def test_minstens_een_rig_metrics_job_is_gevonden(jobs_per_overlay: dict[str, list[dict]]) -> None:
    """Zonder deze vloer wordt de grendel hierboven groen op de dag dat niets het endpoint
    meer scrapet, of op een render die geen enkele overlay oplevert."""
    gevonden = [
        f"{overlay}/{job['job_name']}"
        for overlay, jobs in jobs_per_overlay.items()
        for job in jobs
        if job.get("metrics_path") in _TOKEN_AFGESCHERMD
    ]
    assert gevonden, f"geen enkele overlay scrapet {_TOKEN_AFGESCHERMD}: grendel meet niets"
