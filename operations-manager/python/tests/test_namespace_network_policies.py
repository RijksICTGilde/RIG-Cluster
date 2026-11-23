"""The namespace-wide NetworkPolicies: default-deny, the baseline for pods without a
deployment label, and access to a dedicated database cluster.

They belong to the project and not to one deployment (RC-503), so they come out of the
project level (`_project`) instead of the directory of the alphabetically first
deployment. There they lived with a deployment that happened to come first: two
deployments in the same namespace managed the same object, and removing the owner took
them along.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from opi.generation.manifests import render_template
from opi.manager.delete_project_manager import DeleteProjectManager
from opi.manager.project.emitters import remove_namespace_network_policies
from opi.manager.project.helm_deployment import HelmDeployment
from opi.manager.project.manifests.application import ApplicationManifests
from opi.manager.project.manifests.repository import RepositoryManifests
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.base import ProjectManifestContext
from opi.services.catalog.platform import PlatformService
from opi.services.catalog.platform.network_policies import legacy_deployment_policy_filenames
from opi.services.registry import project_manifest_services
from tests.deployment_removal_helper import remove_from_project_file_edit
from tests.test_delete_project_manager import _make_deployment, _make_project_data, _make_project_manager_mock
from tests.test_deployment_removal_one_path import _argo, _store

#: Where the helm paths bind their module names, and so where a patch has to land.
_HELM = "opi.manager.project.helm_deployment"

CLUSTER = "odcn-production"
NAMESPACE = "rig-prd-demo"

PROJECT_DATA: dict[str, Any] = {
    "name": "demo",
    "deployments": [
        {"name": "web", "cluster": CLUSTER, "namespace": "demo"},
        {"name": "api", "cluster": CLUSTER, "namespace": "demo"},
    ],
}

#: The three files as the project level writes them.
PROJECT_LEVEL_FILES = [
    "platform-default-deny-network-policy",
    "platform-unlabeled-baseline-network-policy",
    "platform-dedicated-database-network-policy",
]


def _project_level_specs() -> dict[str, Any]:
    ctx = ProjectManifestContext(project_name="demo", project_data=PROJECT_DATA, cluster=CLUSTER, namespace=NAMESPACE)
    return {spec.filename: spec for spec in PlatformService().contribute_project_manifests(ctx)}


def _rendered(filename: str) -> dict[str, Any]:
    spec = _project_level_specs()[filename]
    return yaml.safe_load(render_template(spec.template_path, spec.values))


class TestTheProjectLevelCarriesThemOnce:
    def test_the_platform_service_contributes_the_three_policies(self):
        specs = _project_level_specs()
        assert [name for name in PROJECT_LEVEL_FILES if name in specs] == PROJECT_LEVEL_FILES

    def test_the_platform_service_is_registered_for_the_project_level(self):
        """Without this registration ``process_project_manifests`` does not write them."""
        assert any(isinstance(service, PlatformService) for service in project_manifest_services())

    def test_the_object_names_stay_the_same(self):
        """Equal names let ArgoCD adopt the existing object instead of adding a second one."""
        specs = _project_level_specs()
        assert [specs[name].values["name"] for name in PROJECT_LEVEL_FILES] == [
            "default-deny-network-policy",
            "unlabeled-baseline-network-policy",
            "dedicated-database-network-policy",
        ]

    def test_the_three_policies_may_not_be_pruned(self):
        """During the move the deployment application loses them from git before the
        project application has adopted them."""
        for name in PROJECT_LEVEL_FILES:
            annotations = _rendered(name)["metadata"]["annotations"]
            assert annotations["argocd.argoproj.io/sync-options"] == "Prune=false"


class TestWhatIsWritten:
    def test_the_default_deny_covers_every_pod_both_ways(self):
        spec = _rendered("platform-default-deny-network-policy")["spec"]
        assert spec["podSelector"] == {}
        assert sorted(spec["policyTypes"]) == ["Egress", "Ingress"]
        assert "ingress" not in spec
        assert "egress" not in spec

    def test_the_baseline_selects_exactly_the_pods_without_a_deployment_label(self):
        spec = _rendered("platform-unlabeled-baseline-network-policy")["spec"]
        assert spec["podSelector"] == {"matchExpressions": [{"key": "deployment", "operator": "DoesNotExist"}]}

    def test_the_database_cluster_is_reachable_from_its_own_namespace(self):
        spec = _rendered("platform-dedicated-database-network-policy")["spec"]
        assert spec["podSelector"] == {"matchExpressions": [{"key": "cnpg.io/cluster", "operator": "Exists"}]}
        own_namespace = [rule for rule in spec["ingress"] if rule["from"] == [{"podSelector": {}}]]
        assert len(own_namespace) == 1
        assert own_namespace[0]["ports"] == [{"protocol": "TCP", "port": 5432}]


class TestNoDeploymentDirectoryCarriesThem:
    def test_an_old_copy_is_cleaned_up(self, tmp_path):
        for filename in legacy_deployment_policy_filenames():
            (tmp_path / filename).write_text("kind: NetworkPolicy\n")

        remove_namespace_network_policies(str(tmp_path))

        assert list(tmp_path.glob("*.yaml")) == []

    def test_a_directory_without_an_old_copy_is_left_alone(self, tmp_path):
        remove_namespace_network_policies(str(tmp_path))
        assert list(tmp_path.iterdir()) == []


def _kustomization_resources(directory: Path) -> set[str]:
    """The resources of a kustomization, or an empty set when there is none."""
    path = directory / "kustomization.yaml"
    if not path.exists():
        return set()
    return set(yaml.safe_load(path.read_text()).get("resources") or [])


def _manager() -> ProjectManager:
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.services.project_key_secret.ProjectKeySecretHandler"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        return ProjectManager()


class TestTheHelmPathsKeepNoReference:
    """The kustomization of a helm or helmfile deployment comes from a list and not from a
    scan of the directory, so a leftover reference to a vanished file makes ArgoCD reject
    the whole render."""

    DEPLOYMENT: ClassVar[dict[str, Any]] = {"name": "web", "cluster": "sandboxed-local", "namespace": "demo"}
    PROJECT_DATA: ClassVar[dict[str, Any]] = {"name": "demo", "config": {"age-public-key": "age1demo"}}

    async def test_a_helm_chart_deployment_does_not_name_the_namespace_policies(self, tmp_path):
        manager = _manager()
        manager.get_contents = AsyncMock(return_value=self.PROJECT_DATA)
        manager._project_file_handler = MagicMock()

        with (
            patch.object(HelmDeployment, "get_helm_values_context", AsyncMock(return_value={})),
            patch.object(HelmDeployment, "create_deployment_secrets", AsyncMock(return_value=[])),
            patch(f"{_HELM}.get_prefixed_namespace", return_value="rig-demo"),
        ):
            await HelmDeployment(manager).process_helm_chart_deployment(
                {**self.DEPLOYMENT, "helm-charts": []}, MagicMock(), str(tmp_path)
            )

        assert _kustomization_resources(tmp_path).isdisjoint(legacy_deployment_policy_filenames())

    async def test_a_helmfile_deployment_does_not_name_the_namespace_policies(self, tmp_path):
        manager = _manager()
        manager.get_contents = AsyncMock(return_value=self.PROJECT_DATA)
        manager._project_file_handler = MagicMock()

        with (
            patch.object(HelmDeployment, "get_helm_values_context", AsyncMock(return_value={})),
            patch.object(HelmDeployment, "create_deployment_secrets", AsyncMock(return_value=[])),
            patch(f"{_HELM}.clone_helmfile_source", AsyncMock(return_value=("unused", None))),
            patch(f"{_HELM}.write_helmfile_custom_files", MagicMock(return_value={})),
            patch(f"{_HELM}.get_prefixed_namespace", return_value="rig-demo"),
            patch(f"{_HELM}.encrypt_to_sops_files_or_fail"),
        ):
            await HelmDeployment(manager).process_helmfile_deployment(
                {**self.DEPLOYMENT, "helmfile": []}, MagicMock(), str(tmp_path)
            )

        assert _kustomization_resources(tmp_path).isdisjoint(legacy_deployment_policy_filenames())


class TestTheComponentPathClearsTheOldCopy:
    """The component path builds its kustomization from a scan of the directory, so an old
    copy left behind would be adopted again. This is the transition of a project that has
    not been reprocessed since RC-503."""

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

    async def test_the_old_copy_leaves_the_directory_and_the_kustomization(self, tmp_path):
        manager = _manager()
        manager.get_contents = AsyncMock(return_value=self.PROJECT_DATA)
        manager.get_name = AsyncMock(return_value="demo")
        manager.get_repository_path = AsyncMock(return_value="")

        git_connector = MagicMock()
        git_connector.get_working_dir = AsyncMock(return_value=str(tmp_path))

        target_path = Path(tmp_path) / "sandboxed-local" / "demo" / "web"
        target_path.mkdir(parents=True)
        for filename in legacy_deployment_policy_filenames():
            (target_path / filename).write_text("kind: NetworkPolicy\n")

        with (
            patch.object(ApplicationManifests, "create_application_manifests", AsyncMock(return_value=[])),
            patch.object(RepositoryManifests, "prune_obsolete_component_manifests", MagicMock()),
            patch("opi.manager.project.manifests.repository.prune_obsolete_service_manifests"),
            patch("opi.manager.project.manifests.repository.get_prefixed_namespace", return_value="rig-demo"),
            patch("opi.manager.project.manifests.repository.apply_rules_to_directory"),
            patch("opi.manager.project.manifests.repository.encrypt_to_sops_files_or_fail"),
        ):
            await manager._process_deployment_manifests(self.DEPLOYMENT, git_connector)

        assert list(target_path.glob("*network-policy.yaml")) == []
        assert _kustomization_resources(target_path).isdisjoint(legacy_deployment_policy_filenames())


class TestRemovingADeploymentLeavesThemStanding:
    """The alphabetically first deployment owned the namespace objects, so removing it took
    the default-deny along. They now live in ``_project``, outside every deployment's
    directory."""

    async def test_the_project_level_survives_removal_of_the_first_deployment(self, tmp_path):
        project_level = tmp_path / "local" / "test-project" / "_project"
        project_level.mkdir(parents=True)
        for name in PROJECT_LEVEL_FILES:
            (project_level / f"{name}.yaml").write_text("kind: NetworkPolicy\n")
        deployment_dir = tmp_path / "local" / "test-project" / "pr-10"
        deployment_dir.mkdir()
        (deployment_dir / "deployment.yaml").write_text("kind: Deployment\n")

        async def _remove_and_push(folder_path: str, _message: str) -> bool:
            shutil.rmtree(tmp_path / folder_path)
            return True

        workspace = AsyncMock()
        workspace.get_working_dir = AsyncMock(return_value=str(tmp_path))
        workspace.remove_and_push = AsyncMock(side_effect=_remove_and_push)

        mock_pm = _make_project_manager_mock()
        mock_pm.get_deployment_workspace = AsyncMock(return_value=workspace)
        deployments = [_make_deployment(name="pr-10"), _make_deployment(name="pr-42")]
        project_data = _make_project_data(deployments=deployments)
        mock_pm.get_contents = AsyncMock(return_value=project_data)

        with (
            patch("opi.manager.project.deployment_removal.create_argo_connector", return_value=_argo()),
            patch("opi.manager.project.deployment_removal.get_project_store", return_value=_store()),
        ):
            await remove_from_project_file_edit(
                DeleteProjectManager(mock_pm), "test-project", deployments[0], project_data
            )

        assert not deployment_dir.exists()
        assert sorted(path.stem for path in project_level.glob("*.yaml")) == sorted(PROJECT_LEVEL_FILES)
