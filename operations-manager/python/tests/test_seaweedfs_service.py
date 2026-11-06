"""De guards van de dienst seaweedfs-storage: waar hij bestaat, en wat hij weigert.

Waar de dienst bestaat is geen eigenschap van de code hier maar van de clusterconfiguratie:
deze toetsen leggen vast dat beide consumenten van ``available_on_cluster`` een cluster
zonder ``seaweedfs_host`` overslaan, en dat is sinds RC-381 alleen nog ``local``.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_project_structure, validate_service_availability
from opi.services.catalog.base import ManifestContext
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from opi.utils.secrets import MinIOSecret, SeaweedFSSecret

DIENST = get_service(ServiceType.SEAWEEDFS_STORAGE)
MINIO = get_service(ServiceType.MINIO_STORAGE)


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
        assert DIENST.available_on_cluster("odcn-production") is True
        assert DIENST.available_on_cluster("odcn-staging") is True
        assert DIENST.available_on_cluster("local") is False

    def test_opslaan_wordt_geweigerd_op_een_cluster_zonder_adres(self) -> None:
        # De tweede consument, en de weigering die telt: de API en een
        # handgeschreven projectbestand komen nooit langs een kaart.
        project = _project(
            [{"name": "dep", "cluster": "local", "namespace": "proj", "components": [{"reference": "web"}]}],
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

    def test_clone_from_wordt_niet_meer_geweigerd(self) -> None:
        # Tot RC-381 was dit een fout: de kloon was gemeten maar niet bedraad.
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
        assert DIENST.validate_project(project) == []

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
            cluster="local",
            get_secret=lambda dep, soort, klasse: creds,
        )
        assert DIENST.build_secret_files(ctx) == []


def _minio_project(
    deployments: list[dict[str, Any]], componenten: list[dict[str, Any]], services: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "name": "proj",
        "services": services if services is not None else ["minio-storage"],
        "components": componenten,
        "deployments": deployments,
    }


class TestVervangenDoorEenOpvolger:
    """RC-382: minio-storage draagt ``replaced_by = seaweedfs-storage``."""

    def test_vervangen_waar_de_opvolger_bestaat(self) -> None:
        assert MINIO.replaced_on_cluster("sandboxed-local") is True

    def test_niet_vervangen_waar_de_opvolger_ontbreekt(self) -> None:
        # Sinds RC-381 is "local" het enige cluster zonder seaweedfs-storage
        # (TestBeschikbaarheid hierboven), dus daar is niets om naar over te stappen.
        assert MINIO.available_on_cluster("local") is True
        assert MINIO.replaced_on_cluster("local") is False

    def test_replacement_name_is_de_weergavenaam_van_de_opvolger(self) -> None:
        assert MINIO.replacement_name() == DIENST.definition.name

    def test_de_opvolger_zelf_is_nooit_vervangen(self) -> None:
        assert DIENST.replaced_by is None
        assert DIENST.replaced_on_cluster("sandboxed-local") is False


class TestValidatieVanEenVervangenDienst:
    """``validate_service_availability`` met zijn tweede, zachtere regel (RC-382):
    een deployment die minio-storage al had, houdt hem; een deployment die hem
    NIEUW zou krijgen op een cluster met de opvolger, wordt geweigerd."""

    def _dep(self, naam: str = "dep", cluster: str = "sandboxed-local") -> dict[str, Any]:
        return {"name": naam, "cluster": cluster, "namespace": "proj", "components": [{"reference": "web"}]}

    def _comp(self, services: list[Any] | None = None) -> list[dict[str, Any]]:
        return [{"name": "web", "services": services if services is not None else ["minio-storage"]}]

    def test_nieuw_project_met_de_vervangen_dienst_wordt_geweigerd(self) -> None:
        project = _minio_project([self._dep()], self._comp())
        fouten = validate_service_availability(project, previous=None)
        assert len(fouten) == 1
        assert "minio-storage" in fouten[0]
        assert "seaweedfs-storage" in fouten[0]
        assert "dep" in fouten[0]

    def test_een_deployment_die_hem_al_had_houdt_hem(self) -> None:
        project = _minio_project([self._dep()], self._comp())
        vorige = _minio_project([self._dep()], self._comp())
        assert validate_service_availability(project, previous=vorige) == []

    def test_een_tweede_deployment_in_een_project_dat_hem_al_had_mag_hem_ook(self) -> None:
        oud_comp = {"name": "oud-comp", "services": ["minio-storage"]}
        nieuw_comp = {"name": "web", "services": ["minio-storage"]}
        oud_dep = {**self._dep("oud"), "components": [{"reference": "oud-comp"}]}
        vorige = {
            "name": "proj",
            "services": ["minio-storage"],
            "components": [oud_comp],
            "deployments": [oud_dep],
        }
        project = {
            "name": "proj",
            "services": ["minio-storage"],
            "components": [oud_comp, nieuw_comp],
            "deployments": [oud_dep, self._dep("nieuw")],
        }

        assert validate_service_availability(project, previous=vorige) == []

    def test_een_gekloonde_deployment_houdt_de_dienst_van_zijn_bron(self) -> None:
        # Een PR-preview: de V2-upsert kloont 'main' naar een nieuwe deployment.
        vorige = _minio_project([self._dep("main")], self._comp())
        project = _minio_project([self._dep("main"), self._dep("pr-803")], self._comp())
        assert validate_service_availability(project, previous=vorige) == []

    def test_een_bestaand_project_zonder_de_dienst_krijgt_hem_niet_erbij(self) -> None:
        vorige = _minio_project([self._dep()], self._comp(services=[]), services=[])
        project = _minio_project([self._dep()], self._comp())

        fouten = validate_service_availability(project, previous=vorige)
        assert len(fouten) == 1
        assert "minio-storage" in fouten[0]
        assert "seaweedfs-storage" in fouten[0]

    def test_op_een_cluster_zonder_opvolger_verandert_er_niets(self) -> None:
        # "local" heeft geen seaweedfs-storage, dus minio-storage is daar niet vervangen:
        # zelfs zonder vorige versie mag een deployment hem gewoon krijgen.
        project = _minio_project([self._dep(cluster="local")], self._comp())
        assert validate_service_availability(project, previous=None) == []

    @pytest.mark.asyncio
    async def test_de_opslagweg_weigert_en_noemt_de_opvolger(self) -> None:
        """De weigering die telt: de wizard, de API en een handgeschreven bestand lopen hier langs."""
        with pytest.raises(ProjectIntegrityError) as fout:
            await validate_project_structure(_minio_project([self._dep()], self._comp()))
        assert ServiceType.MINIO_STORAGE.value in str(fout.value)
        assert ServiceType.SEAWEEDFS_STORAGE.value in str(fout.value)

    @pytest.mark.asyncio
    async def test_de_opslagweg_geeft_de_vorige_versie_door(self) -> None:
        """Zonder die doorgifte zou elke save van een bestaand MinIO-project geweigerd worden."""
        await validate_project_structure(
            _minio_project([self._dep()], self._comp()),
            previous=_minio_project([self._dep()], self._comp()),
        )
