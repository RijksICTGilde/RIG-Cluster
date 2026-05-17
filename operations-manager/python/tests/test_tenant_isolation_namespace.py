"""Tenant-isolation regression tests.

Two confirmed cross-tenant vulnerabilities are covered here:

1. ``ProjectManager.get_deployments`` must pin every deployment namespace to
   the project name. A project file is attacker-controlled; an explicit
   ``namespace`` pointing at another project would let OPI label and operate
   on a victim namespace and generate ArgoCD resources targeting it.

2. The wizard "create project" flow must not overwrite an existing project
   file. Without an existence check a tenant could submit the wizard with
   another tenant's project name and silently take over that project.
"""

from unittest.mock import AsyncMock

import pytest
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
