"""De generieke emitter van het projectniveau, en zijn prune.

Het projectniveau is een tweede exemplaar van een bestaand begrip: de diensten bepalen wat
er komt te staan (``contribute_project_manifests``), de emitter schrijft het weg, en de
prune haalt weg wat deze run niet opnieuw maakte -- anders loopt de map nooit leeg als een
dienst uitgaat.

Wat hier het meest toe doet is de prune tegenover de SOPS-encryptie. Een dienstbestand met
een secret staat na de vorige run als ``.sops.yaml`` op schijf en wordt deze run als
``.to-sops.yaml`` geschreven. Ziet de prune die versleutelde kopie als overbodig, dan
verdwijnt hij vlak voor de encryptie en heeft de skip-if-unchanged niets meer om tegen te
vergelijken: dan herschrijft SOPS het blok bij elke run, en dat is precies de churn die
eerder is opgeruimd.
"""

from __future__ import annotations

import os
from typing import Any

from opi.manager.project_manager import _select_obsolete_service_manifests
from opi.services.services_enums import ServiceType

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
        """De emitter zet BEIDE namen in de gewenste toestand, precies hiervoor."""
        _touch(
            tmp_path,
            "image-registries-demo-registry.sops.yaml",
            "image-registries-demo-registry.to-sops.yaml",
        )
        generated = {"image-registries-demo-registry.to-sops.yaml", "image-registries-demo-registry.sops.yaml"}
        assert _select_obsolete_service_manifests(str(tmp_path), PREFIXES, generated) == []

    def test_alleen_de_to_sops_naam_zou_de_vorige_ciphertext_opeten(self, tmp_path: Any) -> None:
        """De tegenproef op de regel hierboven: zonder de tweede naam gaat hij wel weg."""
        _touch(
            tmp_path,
            "image-registries-demo-registry.sops.yaml",
            "image-registries-demo-registry.to-sops.yaml",
        )
        generated = {"image-registries-demo-registry.to-sops.yaml"}
        assert _select_obsolete_service_manifests(str(tmp_path), PREFIXES, generated) == [
            "image-registries-demo-registry.sops.yaml"
        ]

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
        from opi.utils.naming import PROJECT_LEVEL_DIR

        assert PROJECT_LEVEL_DIR.startswith("_")

    def test_de_gereserveerde_deploymentnaam_hoort_bij_de_applicatienaam(self) -> None:
        from opi.utils.naming import RESERVED_DEPLOYMENT_NAMES, generate_argocd_project_application_name

        assert generate_argocd_project_application_name("demo") == "demo-project"
        assert "project" in RESERVED_DEPLOYMENT_NAMES

    def test_een_deployment_die_project_heet_wordt_geweigerd(self) -> None:
        import asyncio

        from opi.core.project_schema import ProjectIntegrityError
        from opi.manager.project_validation import validate_project_structure

        data = {
            "name": "demo",
            "components": [{"name": "web"}],
            "deployments": [
                {"name": "project", "cluster": "odcn-production", "namespace": "demo", "components": []}
            ],
        }
        try:
            asyncio.run(validate_project_structure(data))
        except ProjectIntegrityError as e:
            assert "gereserveerde" in str(e)
        else:
            raise AssertionError("een deployment met de naam 'project' had geweigerd moeten worden")

    def test_een_gewone_deploymentnaam_mag_gewoon(self) -> None:
        import asyncio

        from opi.manager.project_validation import validate_project_structure

        data = {
            "name": "demo",
            "components": [{"name": "web"}],
            "deployments": [{"name": "prod", "cluster": "odcn-production", "namespace": "demo", "components": []}],
        }
        asyncio.run(validate_project_structure(data))


class TestDeArgoApplicatie:
    def test_het_projectniveau_gaat_op_wave_0_en_een_deployment_op_1(self) -> None:
        """Ordening, geen gereedheid: wat namespace-breed is staat er voor de pods die het
        nodig hebben."""
        import yaml

        from opi.generation.manifests import render_template

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
