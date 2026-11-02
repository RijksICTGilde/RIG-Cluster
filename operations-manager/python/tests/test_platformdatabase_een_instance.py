"""`rig-db`, de ene PostgreSQL voor Keycloak, Forgejo, OPI en alle projectdatabases, draait op
elk cluster met een instance.

Dit zijn MANIFEST-toetsen, geen Python: een overlay die de PDB aan laat bij een instance
rendert zonder fout, maar Kyverno weigert hem en een node-drain blijft hangen. Zie
features/platformdatabase-een-instance.md.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DATABASE = REPO_ROOT / "infrastructure/bootstrap/infrastructure/postgresql/database"
BASE_CLUSTER = DATABASE / "base/cluster.yaml"
OVERLAYS = ["sandboxed-local", "odcn-staging", "odcn"]
ODCN_OVERLAYS = ["odcn-staging", "odcn"]


def _rendered_cluster(overlay: str) -> dict:
    proc = subprocess.run(
        ["kustomize", "build", str(DATABASE / "overlays" / overlay)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return next(
        d for d in yaml.safe_load_all(proc.stdout) if d and d["kind"] == "Cluster" and d["metadata"]["name"] == "rig-db"
    )


@pytest.fixture
def kustomize_aanwezig() -> None:
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")


def test_de_basis_draait_een_instance() -> None:
    assert yaml.safe_load(BASE_CLUSTER.read_text())["spec"]["instances"] == 1


@pytest.mark.usefixtures("kustomize_aanwezig")
@pytest.mark.parametrize("overlay", OVERLAYS)
def test_elk_cluster_draait_een_instance(overlay: str) -> None:
    spec = _rendered_cluster(overlay)["spec"]
    assert spec["instances"] == 1
    assert "affinity" not in spec


@pytest.mark.usefixtures("kustomize_aanwezig")
@pytest.mark.parametrize("overlay", ODCN_OVERLAYS)
def test_odcn_zet_de_pdb_uit(overlay: str) -> None:
    """Kyverno (`pdb-cnpg-cluster-validation`) eist dit onder twee instances."""
    assert _rendered_cluster(overlay)["spec"]["enablePDB"] is False
