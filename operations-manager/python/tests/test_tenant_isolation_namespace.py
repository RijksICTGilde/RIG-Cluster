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


# ---------------------------------------------------------------------------
# GAT 3 (review augmentation): extract_deployment_namespace enforces the pin
#
# Eight callsites (3 in backup_router, 2 in restore_router, 3 in backup_tasks,
# 1 in router_detail_edit) read the namespace via this helper without going
# through enforce_namespace_pin. The helper itself now enforces the same
# invariant: declared namespace must equal project name; default when absent.
# ---------------------------------------------------------------------------


class TestExtractDeploymentNamespacePinned:
    """The helper must pin the namespace, not return the raw declared value."""

    def _handler(self):
        from opi.handlers.project_file_handler import ProjectFileHandler

        return ProjectFileHandler.__new__(ProjectFileHandler)

    def test_declared_matching_namespace_returns_project_name(self) -> None:
        project_data = {
            "name": "my-project",
            "deployments": [{"name": "prod", "namespace": "my-project"}],
        }
        assert self._handler().extract_deployment_namespace(project_data, "prod") == "my-project"

    def test_missing_namespace_defaults_to_project_name(self) -> None:
        project_data = {
            "name": "my-project",
            "deployments": [{"name": "prod"}],
        }
        assert self._handler().extract_deployment_namespace(project_data, "prod") == "my-project"

    def test_mismatched_namespace_raises_value_error(self) -> None:
        """Cross-tenant: attacker sets namespace=victim-project on his own YAML."""
        project_data = {
            "name": "attacker-project",
            "deployments": [{"name": "prod", "namespace": "victim-project"}],
        }
        with pytest.raises(ValueError, match="namespace") as exc:
            self._handler().extract_deployment_namespace(project_data, "prod")
        assert "attacker-project" in str(exc.value)
        assert "victim-project" in str(exc.value)

    def test_missing_deployment_returns_none(self) -> None:
        project_data = {
            "name": "my-project",
            "deployments": [{"name": "prod"}],
        }
        assert self._handler().extract_deployment_namespace(project_data, "nonexistent") is None

    def test_multiple_deployments_only_the_named_one_is_inspected(self) -> None:
        """A later deployment with a mismatched namespace must not raise when
        the caller asks for a different (well-formed) deployment."""
        project_data = {
            "name": "my-project",
            "deployments": [
                {"name": "prod", "namespace": "my-project"},
                {"name": "staging", "namespace": "victim-project"},
            ],
        }
        assert self._handler().extract_deployment_namespace(project_data, "prod") == "my-project"
        with pytest.raises(ValueError, match="namespace"):
            self._handler().extract_deployment_namespace(project_data, "staging")


# ---------------------------------------------------------------------------
# GAT 1 (review augmentation): handle_create_project existence check
#
# Mirrors simple_background.process_project_background's existence check.
# Without it, a TaskType.CREATE_PROJECT submission with another tenant's
# project name silently overwrites that tenant's project file.
# ---------------------------------------------------------------------------


class TestHandleCreateProjectExistenceCheck:
    """A create_project task for an existing project name must fail fast."""

    @pytest.mark.asyncio
    async def test_create_project_blocked_when_file_already_exists(self, monkeypatch) -> None:
        from opi.core import task_handlers_project

        # GitConnector instance whose file_exists returns True for any path.
        fake_connector = AsyncMock()
        fake_connector.file_exists = AsyncMock(return_value=True)
        fake_connector.create_or_update_file = AsyncMock()

        class _FakeGitConnector:
            def __init__(self, *args, **kwargs):
                pass

            def __new__(cls, *args, **kwargs):
                return fake_connector

        monkeypatch.setattr("opi.connectors.git.GitConnector", _FakeGitConnector)

        # Stub the small helpers used before the file_exists check.
        monkeypatch.setattr("opi.utils.project_utils.validate_project_name", lambda name: True)

        # Progress object the handler calls into.
        progress = AsyncMock()
        progress.add_task = lambda *_: object()
        progress.update_current_step = lambda *_: None
        progress.complete_task = lambda *_: None
        progress.fail_task = lambda *_: None
        progress.fail_project = lambda *_: None

        payload = {
            "project_name": "victim-project",
            "yaml_content": "name: victim-project\n",
            "is_new_project": True,
        }
        result = await task_handlers_project.handle_create_project(payload, progress)

        assert result["status"] == "failed"
        assert "bestaat al" in result["error"]
        # The destructive call must not have been made.
        fake_connector.create_or_update_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_edit_flow_overwrites_existing_file(self, monkeypatch) -> None:
        """Edit flows (no is_new_project flag) must NOT trip the existence
        check -- they legitimately overwrite an existing project file."""
        from opi.core import task_handlers_project

        fake_connector = AsyncMock()
        fake_connector.file_exists = AsyncMock(return_value=True)
        fake_connector.create_or_update_file = AsyncMock()

        class _FakeGitConnector:
            def __new__(cls, *args, **kwargs):
                return fake_connector

        monkeypatch.setattr("opi.connectors.git.GitConnector", _FakeGitConnector)
        monkeypatch.setattr("opi.utils.project_utils.validate_project_name", lambda name: True)

        # Stub ProjectManager so the handler doesn't try to process the project.
        # We only care that the write happened (and the existence check did not block).
        class _FakeProjectManager:
            def __init__(self, *args, **kwargs):
                pass

            async def process_project_from_git(self, *_args, **_kwargs):
                return None  # short-circuit; tested logic is already past

            async def close(self):
                pass

        monkeypatch.setattr("opi.manager.project_manager.ProjectManager", _FakeProjectManager)

        progress = AsyncMock()
        progress.add_task = lambda *_: object()
        progress.update_current_step = lambda *_: None
        progress.complete_task = lambda *_: None
        progress.fail_task = lambda *_: None
        progress.fail_project = lambda *_: None
        progress.add_subtask = lambda *_args, **_kwargs: object()

        # No is_new_project flag = edit flow.
        payload = {
            "project_name": "existing-project",
            "yaml_content": "name: existing-project\n",
        }
        await task_handlers_project.handle_create_project(payload, progress)

        # The write MUST have happened despite the file existing.
        fake_connector.create_or_update_file.assert_called_once()
