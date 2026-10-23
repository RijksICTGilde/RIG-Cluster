"""De guards van de dienst seaweedfs-storage: waar hij bestaat, en wat hij weigert.

Productie verandert niet, en dat is geen eigenschap van de code hier maar van de
clusterconfiguratie: deze toetsen leggen vast dat beide consumenten van
``available_on_cluster`` de dienst daar niet zien.
"""

from __future__ import annotations

from typing import Any

from opi.manager.project_validation import validate_service_availability
from opi.services.catalog.base import ManifestContext
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from opi.utils.secrets import MinIOSecret, SeaweedFSSecret

DIENST = get_service(ServiceType.SEAWEEDFS_STORAGE)


def _project(deployments: list[dict[str, Any]], componenten: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "proj",
        "services": ["seaweedfs-storage"],
        "components": componenten,
        "deployments": deployments,
    }


class TestBeschikbaarheid:
    def test_alleen_op_een_cluster_met_een_seaweedfs_adres(self) -> None:
        assert DIENST.available_on_cluster("sandboxed-local") is True
        assert DIENST.available_on_cluster("odcn-production") is False
        assert DIENST.available_on_cluster("local") is False

    def test_opslaan_wordt_geweigerd_op_productie(self) -> None:
        # De tweede consument, en de weigering die telt: de API en een
        # handgeschreven projectbestand komen nooit langs een kaart.
        project = _project(
            [{"name": "dep", "cluster": "odcn-production", "namespace": "proj", "components": [{"reference": "web"}]}],
            [{"name": "web", "services": ["seaweedfs-storage"]}],
        )
        fouten = validate_service_availability(project)
        assert any("seaweedfs-storage" in fout for fout in fouten)

    def test_opslaan_mag_op_de_sandbox(self) -> None:
        project = _project(
            [{"name": "dep", "cluster": "sandboxed-local", "namespace": "proj", "components": [{"reference": "web"}]}],
            [{"name": "web", "services": ["seaweedfs-storage"]}],
        )
        assert validate_service_availability(project) == []


class TestValidateProject:
    def test_niet_samen_met_minio_op_een_deployment(self) -> None:
        # Beide diensten geven dezelfde OBJECT_STORE_*-variabelen, dus in een pod zou de
        # ene de andere overschrijven.
        project = _project(
            [{"name": "dep", "cluster": "sandboxed-local", "namespace": "proj", "components": [{"reference": "web"}]}],
            [{"name": "web", "services": ["seaweedfs-storage", "minio-storage"]}],
        )
        fouten = DIENST.validate_project(project)
        assert len(fouten) == 1
        assert "minio-storage" in fouten[0]
        assert "dep" in fouten[0]

    def test_geen_clone_from(self) -> None:
        project = _project(
            [
                {
                    "name": "dep",
                    "cluster": "sandboxed-local",
                    "namespace": "proj",
                    "components": [{"reference": "web"}],
                    "clone-from": {"type": "deployment", "reference": "ander"},
                }
            ],
            [{"name": "web", "services": ["seaweedfs-storage"]}],
        )
        fouten = DIENST.validate_project(project)
        assert len(fouten) == 1
        assert "clone-from" in fouten[0]

    def test_een_project_zonder_de_dienst_levert_niets_op(self) -> None:
        project = _project(
            [{"name": "dep", "cluster": "sandboxed-local", "namespace": "proj", "components": [{"reference": "web"}]}],
            [{"name": "web", "services": ["minio-storage"]}],
        )
        assert DIENST.validate_project(project) == []

    def test_een_ander_deployment_met_minio_mag(self) -> None:
        project = _project(
            [
                {"name": "a", "cluster": "sandboxed-local", "namespace": "proj", "components": [{"reference": "web"}]},
                {"name": "b", "cluster": "sandboxed-local", "namespace": "proj", "components": [{"reference": "oud"}]},
            ],
            [
                {"name": "web", "services": ["seaweedfs-storage"]},
                {"name": "oud", "services": ["minio-storage"]},
            ],
        )
        assert DIENST.validate_project(project) == []


class TestSecret:
    def test_dezelfde_sleutelnamen_als_minio(self) -> None:
        gemeen = {"access_key": "proj_dep", "secret_key": "Geheim123", "bucket_name": "proj-dep", "region": "us-east-1"}
        minio = MinIOSecret(host="minio", port=9000, **gemeen).to_k8s_secret_data()
        seaweedfs = SeaweedFSSecret(host="seaweedfs", port=8333, **gemeen).to_k8s_secret_data()
        assert sorted(seaweedfs) == sorted(minio)
        assert SeaweedFSSecret.get_secret_name("dep") == "dep-seaweedfs"

    def test_build_secret_files_zet_host_en_poort_van_het_cluster(self) -> None:
        creds = SeaweedFSSecret(
            host="genegeerd", port=1, access_key="proj_dep", secret_key="Geheim123", bucket_name="proj-dep"
        )
        ctx = ManifestContext(
            deployment_name="dep",
            unique_name="dep-web",
            project_data={},
            cluster="sandboxed-local",
            get_secret=lambda dep, soort, klasse: creds if soort == "seaweedfs" else None,
        )
        [spec] = DIENST.build_secret_files(ctx)
        assert spec.secret_name == "dep-seaweedfs"
        assert spec.secret_pairs["OBJECT_STORE_HOST"] == "seaweedfs.rig-system.svc.cluster.local"
        assert spec.secret_pairs["OBJECT_STORE_PORT"] == "8333"
        assert spec.secret_pairs["OBJECT_STORE_ENDPOINT_URL"] == "http://seaweedfs.rig-system.svc.cluster.local:8333"
        # De aliasbucket volgt de categorie waar beide object-storage-diensten op
        # collapsen; "seaweedfs" zou een bucket zijn die niemand vult.
        assert spec.secret_type == "minio"

    def test_geen_secret_op_een_cluster_zonder_seaweedfs(self) -> None:
        creds = SeaweedFSSecret(
            host="h", port=8333, access_key="proj_dep", secret_key="Geheim123", bucket_name="proj-dep"
        )
        ctx = ManifestContext(
            deployment_name="dep",
            unique_name="dep-web",
            project_data={},
            cluster="odcn-production",
            get_secret=lambda dep, soort, klasse: creds,
        )
        assert DIENST.build_secret_files(ctx) == []
