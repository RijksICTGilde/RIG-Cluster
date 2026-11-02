"""De redundantie van `rig-db`, de ene PostgreSQL voor Keycloak, Forgejo, OPI en alle
projectdatabases.

Dit zijn MANIFEST-toetsen, geen Python: wat hier fout kan gaan (instances terug naar 1 met
een PDB die aanblijft, of een overlay die zijn anti-affinity verliest) rendert zonder fout
maar laat het platform zonder redundantie. Zie features/platformdatabase-hoge-beschikbaarheid.md.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DATABASE = REPO_ROOT / "infrastructure/bootstrap/infrastructure/postgresql/database"
ODCN_OVERLAY = DATABASE / "overlays/odcn"
SANDBOX_OVERLAY = DATABASE / "overlays/sandboxed-local"
BASE_CLUSTER = DATABASE / "base/cluster.yaml"


def _rendered_cluster(overlay: Path) -> dict:
    proc = subprocess.run(
        ["kustomize", "build", str(overlay)],
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


class TestDeBasisEnDeSandboxBlijvenOpEenInstance:
    """Een Kind-cluster heeft een node: een tweede instance levert daar geen redundantie op."""

    def test_de_basis_draait_een_instance(self) -> None:
        spec = yaml.safe_load(BASE_CLUSTER.read_text())["spec"]
        assert spec["instances"] == 1

    @pytest.mark.usefixtures("kustomize_aanwezig")
    def test_de_sandbox_erft_een_instance_van_de_basis(self) -> None:
        spec = _rendered_cluster(SANDBOX_OVERLAY)["spec"]
        assert spec["instances"] == 1
        assert "affinity" not in spec, "de sandbox-overlay zet geen anti-affinity"


@pytest.mark.usefixtures("kustomize_aanwezig")
class TestOdcnDraaitMetTweeInstancesEnEenPdb:
    """RC-308: nodeonderhoud, een CNPG-upgrade of een postmaster-herstart wordt zo een
    switchover in plaats van downtime voor het hele platform."""

    def test_odcn_draait_twee_instances(self) -> None:
        assert _rendered_cluster(ODCN_OVERLAY)["spec"]["instances"] == 2

    def test_odcn_zet_de_pdb_aan(self) -> None:
        """Alleen toegestaan vanaf twee instances; zie `pdb-cnpg-cluster-validation`."""
        assert _rendered_cluster(ODCN_OVERLAY)["spec"]["enablePDB"] is True

    def test_odcn_dwingt_de_replica_naar_een_andere_node(self) -> None:
        """`preferred` en niet `required`: het aantal workernodes op ODCN ligt niet in
        deze repo vast, en `required` laat de replica eeuwig Pending bij te weinig nodes."""
        affinity = _rendered_cluster(ODCN_OVERLAY)["spec"]["affinity"]
        assert affinity == {
            "enablePodAntiAffinity": True,
            "podAntiAffinityType": "preferred",
            "topologyKey": "kubernetes.io/hostname",
        }
