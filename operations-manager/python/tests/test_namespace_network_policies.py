"""De namespace-brede NetworkPolicies: default-deny, de baseline zonder deployment-label
en de toegang tot een toegewijd databasecluster.

Ze horen per namespace en niet per deployment. Zonder een eigenaar zetten twee
deployments in dezelfde namespace hetzelfde object in twee ArgoCD-applicaties, en dan
vecht de ene applicatie met de andere over een resource die ze allebei bezitten.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from opi.generation.manifests import ManifestGenerator
from opi.manager.project_manager import ProjectManager
from opi.utils.project_utils import namespace_level_deployment

CLUSTER = "odcn-production"
NAMESPACE = "rig-prd-demo"

PROJECT_DATA: dict[str, Any] = {
    "name": "demo",
    "deployments": [
        {"name": "web", "cluster": CLUSTER, "namespace": "demo"},
        {"name": "api", "cluster": CLUSTER, "namespace": "demo"},
        {"name": "acc", "cluster": CLUSTER, "namespace": "demo-acc"},
    ],
}


def _emit(deployment_name: str, output_dir: str) -> list[str]:
    manager = ProjectManager.__new__(ProjectManager)
    manager._manifest_generator = ManifestGenerator()
    deployment = next(d for d in PROJECT_DATA["deployments"] if d["name"] == deployment_name)
    return manager._emit_namespace_network_policies(
        project_data=PROJECT_DATA,
        project_name="demo",
        deployment=deployment,
        cluster_name=CLUSTER,
        namespace=NAMESPACE,
        output_dir=output_dir,
    )


class TestEenEigenaarPerNamespace:
    def test_de_alfabetisch_eerste_deployment_in_de_namespace_is_de_eigenaar(self):
        owner = namespace_level_deployment(PROJECT_DATA["deployments"], CLUSTER, "demo")
        assert owner is not None
        assert owner["name"] == "api"

    def test_een_namespace_zonder_deployment_heeft_geen_eigenaar(self):
        assert namespace_level_deployment(PROJECT_DATA["deployments"], CLUSTER, "leeg") is None

    def test_een_gelijknamige_namespace_op_een_ander_cluster_telt_niet_mee(self):
        """Elke OPI-instantie beheert maar één cluster (distributed model), maar het
        projectbestand noemt alle clusters van het project in één lijst. Zonder het
        cluster-filter zou een namespace op cluster A een eigenaar krijgen die op
        cluster B draait, en die schrijft daar niets."""
        deployments = [*PROJECT_DATA["deployments"], {"name": "aaa", "cluster": "sandboxed-local", "namespace": "demo"}]
        owner = namespace_level_deployment(deployments, CLUSTER, "demo")
        assert owner is not None
        assert owner["name"] == "api"

    def test_twee_deployments_in_een_namespace_leveren_een_default_deny_en_een_baseline(self, tmp_path):
        eigenaar = tmp_path / "api"
        ander = tmp_path / "web"
        ander.mkdir()
        geschreven = _emit("api", str(eigenaar)) + _emit("web", str(ander))

        assert sorted(geschreven) == [
            "dedicated-database-network-policy.yaml",
            "default-deny-network-policy.yaml",
            "unlabeled-baseline-network-policy.yaml",
        ]
        assert list(ander.glob("*.yaml")) == []

    def test_een_deployment_dat_de_eigenaar_niet_meer_is_ruimt_zijn_kopie_op(self, tmp_path):
        achtergebleven = tmp_path / "default-deny-network-policy.yaml"
        achtergebleven.write_text("kind: NetworkPolicy\n")

        assert _emit("web", str(tmp_path)) == []
        assert not achtergebleven.exists()


class TestWatErGeschrevenWordt:
    @staticmethod
    def _documenten(tmp_path) -> dict[str, dict[str, Any]]:
        _emit("api", str(tmp_path))
        return {
            os.path.basename(path): yaml.safe_load(open(path)) for path in [str(p) for p in tmp_path.glob("*.yaml")]
        }

    def test_de_default_deny_dekt_elke_pod_in_beide_richtingen(self, tmp_path):
        spec = self._documenten(tmp_path)["default-deny-network-policy.yaml"]["spec"]
        assert spec["podSelector"] == {}
        assert sorted(spec["policyTypes"]) == ["Egress", "Ingress"]
        assert "ingress" not in spec
        assert "egress" not in spec

    def test_de_baseline_selecteert_precies_de_pods_zonder_deployment_label(self, tmp_path):
        spec = self._documenten(tmp_path)["unlabeled-baseline-network-policy.yaml"]["spec"]
        assert spec["podSelector"] == {"matchExpressions": [{"key": "deployment", "operator": "DoesNotExist"}]}

    def test_het_databasecluster_is_bereikbaar_vanuit_de_eigen_namespace(self, tmp_path):
        spec = self._documenten(tmp_path)["dedicated-database-network-policy.yaml"]["spec"]
        assert spec["podSelector"] == {"matchExpressions": [{"key": "cnpg.io/cluster", "operator": "Exists"}]}
        eigen_namespace = [rule for rule in spec["ingress"] if rule["from"] == [{"podSelector": {}}]]
        assert len(eigen_namespace) == 1
        assert eigen_namespace[0]["ports"] == [{"protocol": "TCP", "port": 5432}]


def _manager() -> ProjectManager:
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        return ProjectManager()


class TestNamespacePolicyFilesBereikenDeKustomization:
    """``_emit_namespace_network_policies`` schrijft de bestanden alleen op schijf; of ze
    ook in de ArgoCD-applicatie van een helm- of helmfile-deployment belanden hangt af
    van het doorgeven van ``namespace_policy_files`` in ``_process_helm_chart_deployment``
    en ``_process_helmfile_deployment``. Zonder die regel staat de policy wel op schijf
    maar niet in de kustomization, en dus niet in de cluster: precies het gat dat deze
    taak dicht (zie de aanleiding in features/restrictive-network-policies.md).
    """

    DEPLOYMENT: ClassVar[dict[str, Any]] = {"name": "web", "cluster": "sandboxed-local", "namespace": "demo"}
    PROJECT_DATA: ClassVar[dict[str, Any]] = {"name": "demo", "config": {"age-public-key": "age1demo"}}

    async def test_een_helm_chart_deployment_neemt_de_namespace_policies_op(self, tmp_path):
        manager = _manager()
        manager.get_contents = AsyncMock(return_value=self.PROJECT_DATA)
        manager._get_helm_values_context = AsyncMock(return_value={})
        manager._create_deployment_secrets = AsyncMock(return_value=[])
        manager._project_file_handler = MagicMock()

        with patch("opi.manager.project_manager.get_prefixed_namespace", return_value="rig-demo"):
            await manager._process_helm_chart_deployment(
                {**self.DEPLOYMENT, "helm-charts": []},
                MagicMock(),
                str(tmp_path),
                ["default-deny-network-policy.yaml"],
            )

        kustomization = yaml.safe_load((tmp_path / "kustomization.yaml").read_text())
        assert "default-deny-network-policy.yaml" in kustomization["resources"]

    async def test_een_helmfile_deployment_neemt_de_namespace_policies_op(self, tmp_path):
        manager = _manager()
        manager.get_contents = AsyncMock(return_value=self.PROJECT_DATA)
        manager._get_helm_values_context = AsyncMock(return_value={})
        manager._create_deployment_secrets = AsyncMock(return_value=[])
        manager._clone_helmfile_source = AsyncMock(return_value=("unused", None))
        manager._write_helmfile_custom_files = MagicMock(return_value={})
        manager._project_file_handler = MagicMock()

        with (
            patch("opi.manager.project_manager.get_prefixed_namespace", return_value="rig-demo"),
            patch("opi.manager.project_manager.encrypt_to_sops_files_or_fail"),
        ):
            await manager._process_helmfile_deployment(
                {**self.DEPLOYMENT, "helmfile": []},
                MagicMock(),
                str(tmp_path),
                ["default-deny-network-policy.yaml"],
            )

        kustomization = yaml.safe_load((tmp_path / "kustomization.yaml").read_text())
        assert "default-deny-network-policy.yaml" in kustomization["resources"]


class TestComponentDeploymentKrijgtOokDeNamespacePolicies:
    """``_process_deployment_manifests`` roept ``_emit_namespace_network_policies`` aan
    voordat hij naar een van de drie takken vertakt (regel 4354-4363), dus ook de gewone
    component-tak krijgt de default-deny en de baseline zonder label. Anders dan bij een
    helm- of helmfile-deployment komt de kustomization hier uit een scan van de map
    (``collect_manifest_files``), dus geen expliciete lijst nodig: de bestanden moeten
    alleen echt op schijf staan voor ``create_kustomization_file`` draait.
    """

    DEPLOYMENT: ClassVar[dict[str, Any]] = {
        "name": "web",
        "cluster": "sandboxed-local",
        "namespace": "demo",
        "repository": "r",
        "components": [],
    }
    PROJECT_DATA: ClassVar[dict[str, Any]] = {
        "name": "demo",
        "deployments": [DEPLOYMENT],
        "config": {"age-public-key": "age1demo"},
    }

    async def test_de_component_tak_roept_de_namespace_policies_aan(self, tmp_path):
        manager = _manager()
        manager.get_contents = AsyncMock(return_value=self.PROJECT_DATA)
        manager.get_name = AsyncMock(return_value="demo")
        manager.get_repository_path = AsyncMock(return_value="")
        manager.create_application_manifests = AsyncMock(return_value=[])
        manager._prune_obsolete_component_manifests = MagicMock()
        manager._prune_obsolete_service_manifests = MagicMock()

        git_connector = MagicMock()
        git_connector.get_working_dir = AsyncMock(return_value=str(tmp_path))

        with (
            patch("opi.manager.project_manager.get_prefixed_namespace", return_value="rig-demo"),
            patch("opi.manager.project_manager.apply_rules_to_directory"),
            patch("opi.manager.project_manager.encrypt_to_sops_files_or_fail"),
            patch.object(
                manager, "_emit_namespace_network_policies", wraps=manager._emit_namespace_network_policies
            ) as spy,
        ):
            await manager._process_deployment_manifests(self.DEPLOYMENT, git_connector)

        spy.assert_called_once()
        assert spy.call_args.kwargs["deployment"] == self.DEPLOYMENT
        assert spy.call_args.kwargs["namespace"] == "rig-demo"

        target_path = Path(spy.call_args.kwargs["output_dir"])
        assert (target_path / "default-deny-network-policy.yaml").exists()
        kustomization = yaml.safe_load((target_path / "kustomization.yaml").read_text())
        assert "default-deny-network-policy.yaml" in kustomization["resources"]
