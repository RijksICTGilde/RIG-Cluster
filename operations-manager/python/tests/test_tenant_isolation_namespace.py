"""Tenant-isolation regression tests.

Two confirmed cross-tenant vulnerabilities are covered here:

1. The deployment namespace must be pinned to the project name on every
   path that turns a project file into namespace or ArgoCD actions. A
   project file is attacker-controlled; an explicit ``namespace`` pointing
   at another project would let OPI label and operate on a victim namespace
   and generate ArgoCD resources targeting it. This is enforced both at
   ``ProjectManager.get_deployments`` (API/wizard path) and at the
   git-monitor path (a project committed directly to git), which reads the
   project file without going through ``get_deployments``.

2. The wizard "create project" flow must not overwrite an existing project
   file. Without an existence check a tenant could submit the wizard with
   another tenant's project name and silently take over that project.
"""

from unittest.mock import AsyncMock

import pytest
from opi.core import git_monitor
from opi.core.simple_background import process_project_yaml_background
from opi.manager.project_manager import ProjectManager


def _make_manager(project_data: dict) -> ProjectManager:
    """Build a ProjectManager without running its real constructor.

    Only ``get_contents`` is needed by ``get_deployments``; stub it so the
    test does not touch git or the filesystem.
    """
    pm = ProjectManager.__new__(ProjectManager)
    pm.get_contents = AsyncMock(return_value=project_data)  # type: ignore[method-assign]
    return pm


class TestNamespacePinning:
    """VULN 1: deployment.namespace must equal the project name."""

    @pytest.mark.asyncio
    async def test_foreign_namespace_is_rejected(self):
        """A deployment targeting another project's namespace must raise."""
        project_data = {
            "name": "attacker-project",
            "deployments": [
                {
                    "name": "evil",
                    "cluster": "local",
                    "namespace": "victim-project",
                }
            ],
        }
        pm = _make_manager(project_data)

        with pytest.raises(ValueError, match="victim-project"):
            await pm.get_deployments(cluster_filter=False)

    @pytest.mark.asyncio
    async def test_matching_namespace_is_allowed(self):
        """The legitimate case (namespace == project name) must pass."""
        project_data = {
            "name": "my-project",
            "deployments": [
                {
                    "name": "web",
                    "cluster": "local",
                    "namespace": "my-project",
                }
            ],
        }
        pm = _make_manager(project_data)

        deployments = await pm.get_deployments(cluster_filter=False)

        assert len(deployments) == 1
        assert deployments[0]["namespace"] == "my-project"

    @pytest.mark.asyncio
    async def test_absent_namespace_defaults_to_project_name(self):
        """A deployment without a namespace must default to the project name."""
        project_data = {
            "name": "my-project",
            "deployments": [
                {
                    "name": "web",
                    "cluster": "local",
                }
            ],
        }
        pm = _make_manager(project_data)

        deployments = await pm.get_deployments(cluster_filter=False)

        assert deployments[0]["namespace"] == "my-project"

    @pytest.mark.asyncio
    async def test_rejection_happens_before_cluster_filter(self):
        """A foreign namespace must be rejected even if the cluster filter
        would otherwise drop the malicious deployment."""
        project_data = {
            "name": "attacker-project",
            "deployments": [
                {
                    "name": "evil",
                    "cluster": "some-other-cluster",
                    "namespace": "victim-project",
                }
            ],
        }
        pm = _make_manager(project_data)

        with pytest.raises(ValueError, match="victim-project"):
            await pm.get_deployments(cluster_filter=True)


class _FakeGitConnector:
    """Minimal stand-in for GitConnector used by the background task."""

    def __init__(self, *, file_exists: bool):
        self._file_exists = file_exists
        self.create_or_update_file = AsyncMock()

    async def file_exists(self, file_path: str) -> bool:
        return self._file_exists

    async def close(self) -> None:
        return None


class _FakeProjectManager:
    """No-op ProjectManager so the test stops at the git write step."""

    def __init__(self, *args, **kwargs):
        pass

    async def process_project_from_git(self, *args, **kwargs) -> bool:
        return True

    async def close(self) -> None:
        return None


class TestProjectTakeoverGuard:
    """VULN 2: the create flow must not overwrite an existing project file."""

    @pytest.fixture(autouse=True)
    def _patch_git_connector(self, monkeypatch):
        """Capture the GitConnector the background task builds."""
        self.created_connectors: list[_FakeGitConnector] = []
        self.next_file_exists = False

        def _factory(*args, **kwargs):
            connector = _FakeGitConnector(file_exists=self.next_file_exists)
            self.created_connectors.append(connector)
            return connector

        monkeypatch.setattr("opi.core.simple_background.GitConnector", _factory)
        monkeypatch.setattr("opi.core.simple_background.ProjectManager", _FakeProjectManager)

    @pytest.mark.asyncio
    async def test_create_with_existing_project_is_rejected(self, monkeypatch):
        """is_new_project=True + existing file -> no overwrite."""
        self.next_file_exists = True

        await process_project_yaml_background(
            task_id="t1",
            project_name="victim-project",
            yaml_content="name: victim-project\n",
            is_new_project=True,
        )

        connector = self.created_connectors[0]
        connector.create_or_update_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_with_new_name_proceeds_to_write(self):
        """is_new_project=True + no existing file -> file is written."""
        self.next_file_exists = False

        await process_project_yaml_background(
            task_id="t2",
            project_name="brand-new-project",
            yaml_content="name: brand-new-project\n",
            is_new_project=True,
        )

        connector = self.created_connectors[0]
        connector.create_or_update_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_existing_project_still_writes(self):
        """The legitimate update flow (is_new_project=False) must still
        rewrite an existing file - the guard only applies to create."""
        self.next_file_exists = True

        await process_project_yaml_background(
            task_id="t3",
            project_name="owned-project",
            yaml_content="name: owned-project\n",
            is_new_project=False,
        )

        connector = self.created_connectors[0]
        connector.create_or_update_file.assert_awaited_once()


class _SpyKubectl:
    """Records every namespace write the git-monitor path attempts."""

    def __init__(self):
        self.namespace_exists = AsyncMock(return_value=False)
        self.apply_manifest = AsyncMock(return_value=True)
        self.apply_label_to_resource = AsyncMock(return_value=True)


class TestGitMonitorNamespacePinning:
    """VULN 1, git-monitor path: a project committed directly to git must
    not be able to create or label another tenant's namespace.

    Regression guard for the bypass where ``git_monitor`` reads the project
    file directly and never goes through ``ProjectManager.get_deployments``,
    so the chokepoint pin alone would not protect this path.
    """

    @pytest.fixture(autouse=True)
    def _spy_kubectl(self, monkeypatch):
        self.kubectl = _SpyKubectl()
        monkeypatch.setattr(
            "opi.core.git_monitor.create_kubectl_connector",
            lambda *a, **k: self.kubectl,
        )

    @pytest.mark.asyncio
    async def test_foreign_namespace_is_rejected_and_nothing_created(self, monkeypatch):
        """A foreign namespace must raise and never touch kubectl."""
        monkeypatch.setattr(git_monitor.settings, "CLUSTER_MANAGER", "local")
        project_data = {
            "name": "attacker-project",
            "deployments": [
                {
                    "name": "evil",
                    "cluster": "local",
                    "namespace": "victim-project",
                }
            ],
        }

        with pytest.raises(ValueError, match="victim-project"):
            await git_monitor.check_and_create_namespaces(project_data)

        self.kubectl.namespace_exists.assert_not_called()
        self.kubectl.apply_manifest.assert_not_called()
        self.kubectl.apply_label_to_resource.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_namespace_defaults_to_project_name(self, monkeypatch):
        """An absent namespace must be pinned to the project name, so the
        namespace acted on derives from the project, not attacker input."""
        monkeypatch.setattr(git_monitor.settings, "CLUSTER_MANAGER", "local")
        monkeypatch.setattr(
            "opi.core.git_monitor.get_prefixed_namespace",
            lambda cluster, ns: ns,
        )
        monkeypatch.setattr(
            "opi.core.git_monitor.get_argo_namespace",
            lambda cluster: "argocd",
        )
        project_data = {
            "name": "my-project",
            "deployments": [{"name": "web", "cluster": "local"}],
        }

        result = await git_monitor.check_and_create_namespaces(project_data)

        assert result is True
        applied_namespace = self.kubectl.apply_manifest.await_args.args[1]["namespace"]
        assert applied_namespace == "my-project"
