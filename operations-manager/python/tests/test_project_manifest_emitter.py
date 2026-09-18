"""De generieke emitter van het projectniveau, en zijn prune.

Het projectniveau is een tweede exemplaar van een bestaand begrip: de diensten bepalen wat
er komt te staan (``contribute_project_manifests``), de emitter schrijft het weg, en de
prune haalt weg wat deze run niet opnieuw maakte, anders loopt de map nooit leeg als een
dienst uitgaat.

Wat hier het meest toe doet is de prune tegenover de SOPS-encryptie. Een dienstbestand met
een secret staat na de vorige run als ``.sops.yaml`` op schijf en wordt deze run als
``.to-sops.yaml`` geschreven. Ziet de prune die versleutelde kopie als overbodig, dan
verdwijnt hij vlak voor de encryptie en heeft de skip-if-unchanged niets meer om tegen te
vergelijken: dan herschrijft SOPS het blok bij elke run, en dat is precies de churn die
eerder is opgeruimd.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from opi.core.project_schema import ProjectIntegrityError
from opi.generation.manifests import render_template
from opi.manager.delete_project_manager import DeleteProjectManager
from opi.manager.project_manager import _select_obsolete_service_manifests
from opi.manager.project_validation import validate_project_structure
from opi.services.services_enums import ServiceType
from opi.utils.naming import (
    PROJECT_LEVEL_DIR,
    RESERVED_DEPLOYMENT_NAMES,
    generate_argocd_project_application_name,
)
from opi.utils.project_utils import project_level_deployment

PREFIXES = {f"{service.value}-" for service in ServiceType}


def _touch(directory: Any, *names: str) -> None:
    for name in names:
        (directory / name).write_text("kind: Secret\n")


class TestDePrune:
    def test_een_bestand_van_een_dienst_die_niets_meer_bijdraagt_gaat_weg(self, tmp_path: Any) -> None:
        _touch(tmp_path, "image-registries-demo-code-overheid-registry.sops.yaml")
        assert _select_obsolete_service_manifests(str(tmp_path), PREFIXES, set()) == [
            "image-registries-demo-code-overheid-registry.sops.yaml"
        ]

    def test_de_versleutelde_kopie_blijft_staan_als_de_dienst_hem_nog_maakt(self, tmp_path: Any) -> None:
        _touch(
            tmp_path,
            "image-registries-demo-registry.sops.yaml",
            "image-registries-demo-registry.to-sops.yaml",
        )
        generated = {"image-registries-demo-registry.to-sops.yaml"}
        assert _select_obsolete_service_manifests(str(tmp_path), PREFIXES, generated) == []

    def test_een_versleutelde_kopie_zonder_to_sops_deze_run_gaat_weg(self, tmp_path: Any) -> None:
        _touch(
            tmp_path,
            "image-registries-demo-registry.sops.yaml",
            "image-registries-oud-registry.sops.yaml",
        )
        generated = {"image-registries-demo-registry.to-sops.yaml"}
        assert _select_obsolete_service_manifests(str(tmp_path), PREFIXES, generated) == [
            "image-registries-oud-registry.sops.yaml"
        ]

    def test_ook_op_deploymentniveau_blijven_keycloak_en_redis_staan(self, tmp_path: Any) -> None:
        _touch(
            tmp_path,
            "productie-keycloak-secret.sops.yaml",
            "productie-keycloak-secret.to-sops.yaml",
            "productie-redis-secret.sops.yaml",
            "productie-redis-secret.to-sops.yaml",
        )
        prefixes = {f"productie-{service.value}-" for service in ServiceType}
        generated = {"productie-keycloak-secret.to-sops.yaml", "productie-redis-secret.to-sops.yaml"}
        assert _select_obsolete_service_manifests(str(tmp_path), prefixes, generated) == []

    def test_de_kustomize_plumbing_wordt_niet_aangeraakt(self, tmp_path: Any) -> None:
        _touch(tmp_path, "kustomization.yaml", "decrypt-sops.yaml")
        assert _select_obsolete_service_manifests(str(tmp_path), PREFIXES, set()) == []

    def test_een_bestand_zonder_dienstnaam_gaat_deze_prune_niet_aan(self, tmp_path: Any) -> None:
        _touch(tmp_path, "iets-anders.yaml")
        assert _select_obsolete_service_manifests(str(tmp_path), PREFIXES, set()) == []

    def test_een_lege_map_is_geen_fout(self, tmp_path: Any) -> None:
        assert _select_obsolete_service_manifests(str(tmp_path / "bestaat-niet"), PREFIXES, set()) == []


class TestDeMapNaam:
    def test_de_underscore_kan_nooit_botsen_met_een_deployment(self) -> None:
        """Een deploymentnaam is een DNS-label en kan geen underscore bevatten."""
        assert PROJECT_LEVEL_DIR.startswith("_")

    def test_de_gereserveerde_deploymentnaam_hoort_bij_de_applicatienaam(self) -> None:
        assert generate_argocd_project_application_name("demo") == "demo-project"
        assert "project" in RESERVED_DEPLOYMENT_NAMES

    def test_een_deployment_die_project_heet_wordt_geweigerd(self) -> None:
        data = {
            "name": "demo",
            "components": [{"name": "web"}],
            "deployments": [{"name": "project", "cluster": "odcn-production", "namespace": "demo", "components": []}],
        }
        with pytest.raises(ProjectIntegrityError, match="gereserveerde"):
            asyncio.run(validate_project_structure(data))

    def test_een_gewone_deploymentnaam_mag_gewoon(self) -> None:
        data = {
            "name": "demo",
            "components": [{"name": "web"}],
            "deployments": [{"name": "prod", "cluster": "odcn-production", "namespace": "demo", "components": []}],
        }
        asyncio.run(validate_project_structure(data))


class TestWieHetProjectniveauDraagt:
    """Het projectniveau moet in PRECIES EEN repository landen, ook als een project
    deployments over twee repo's heeft: er is een ArgoCD-applicatie die ernaar wijst, en
    een tweede kopie zou zonder eigenaar blijven rondslingeren. Daarom maken de schrijver
    en de applicatie dezelfde keuze, met dezelfde functie."""

    def test_de_alfabetisch_eerste_deployment_wijst_de_repository_aan(self) -> None:
        deployments = [
            {"name": "productie", "repository": "b"},
            {"name": "acceptatie", "repository": "a"},
        ]
        gekozen = project_level_deployment(deployments)
        assert gekozen is not None
        assert gekozen["repository"] == "a"

    def test_de_volgorde_in_het_bestand_beslist_niet(self) -> None:
        """De tegenproef: omgekeerd ingevoerd komt dezelfde keuze eruit, anders zou een
        herschikking van het projectbestand de map laten verhuizen."""
        omgekeerd = [{"name": "acceptatie", "repository": "a"}, {"name": "productie", "repository": "b"}]
        gekozen = project_level_deployment(omgekeerd)
        assert gekozen is not None
        assert gekozen["repository"] == "a"

    def test_zonder_deployments_is_er_geen_eigenaar(self) -> None:
        assert project_level_deployment([]) is None

    def test_een_deployment_zonder_naam_telt_niet_mee(self) -> None:
        assert project_level_deployment([{"repository": "a"}]) is None

    def test_de_verwijderaar_zoekt_in_dezelfde_repository_als_de_schrijver(self, tmp_path: Any) -> None:
        """De derde kant. Koos de verwijderaar op bestandsvolgorde, dan ging hij bij twee
        repositories in de VERKEERDE repo zoeken, en dan blijft de map met zijn
        SOPS-secrets staan terwijl de verwijdering 'not_found' meldt."""
        # De map staat in repo 'a', de repository van de ALFABETISCH EERSTE deployment.
        (tmp_path / "a" / "odcn-production" / "demo" / PROJECT_LEVEL_DIR).mkdir(parents=True)

        gevraagd: list[str] = []

        async def _connector(name: str, _config: dict[str, Any]) -> Any:
            gevraagd.append(name)
            connector = MagicMock()
            connector.ensure_repo_cloned = AsyncMock()
            connector.get_working_dir = AsyncMock(return_value=str(tmp_path / name))
            connector.commit_and_push_changes = AsyncMock()
            return connector

        manager = DeleteProjectManager.__new__(DeleteProjectManager)
        manager.project_manager = MagicMock()
        manager.project_manager.get_git_connector_for_deployment = _connector

        project_data = {
            "repositories": [{"name": "a", "path": ""}, {"name": "b", "path": ""}],
        }
        # In BESTANDSvolgorde staat 'productie' (repo b) vooraan; alfabetisch wint 'acceptatie'.
        deployments = [
            {"name": "productie", "repository": "b"},
            {"name": "acceptatie", "repository": "a"},
        ]
        results: dict[str, Any] = {"operations": [], "errors": []}
        asyncio.run(manager._delete_project_level_folder("demo", "odcn-production", project_data, deployments, results))

        assert gevraagd == ["a"]
        assert results["errors"] == []
        assert results["operations"] == [
            {
                "type": "project_level_folder_deletion",
                "target": f"odcn-production/demo/{PROJECT_LEVEL_DIR}",
                "status": "success",
            }
        ]
        assert not (tmp_path / "a" / "odcn-production" / "demo" / PROJECT_LEVEL_DIR).exists()


class TestDeArgoApplicatie:
    def test_het_projectniveau_gaat_op_wave_0_en_een_deployment_op_1(self) -> None:
        """Ordening, geen gereedheid: wat namespace-breed is staat er voor de pods die het
        nodig hebben."""

        base = {
            "name": "demo-app",
            "namespace": "argocd",
            "argo_project": "demo",
            "repoURL": "https://git/repo.git",
            "targetRevision": "main",
            "repoPath": "odcn-production/demo/_project",
            "labels": {"project": "demo"},
            "destination": {"namespace": "rig-prd-demo"},
        }
        project_level = yaml.safe_load(render_template("argocd-application.yaml.jinja", {**base, "sync_wave": 0}))
        deployment_level = yaml.safe_load(render_template("argocd-application.yaml.jinja", base))
        assert project_level["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "0"
        assert deployment_level["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "1"
