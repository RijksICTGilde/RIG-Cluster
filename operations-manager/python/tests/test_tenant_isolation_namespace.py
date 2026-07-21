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

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from opi.api.restore_router import _require_namespace_owned_by_project
from opi.core import git_monitor
from opi.core.cluster_config import get_prefixed_namespace
from opi.manager.backup import RestoreResult
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


class _SpyKubectl:
    """Records every namespace write the git-monitor path attempts."""

    def __init__(self):
        self.namespace_exists = AsyncMock(return_value=False)
        self.apply_manifest = AsyncMock(return_value=None)  # success = no raise
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
        # The edit flow now persists through the single validated path
        # (save_and_commit_project), not the raw create_or_update_file commit.
        saved_calls: list[tuple] = []

        class _FakeProjectManager:
            def __init__(self, *args, **kwargs):
                pass

            async def save_and_commit_project(self, project_data, commit_message, **_kwargs):
                saved_calls.append((project_data, commit_message))

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

        # The write MUST have happened despite the file existing, now via the
        # single validated save path rather than a raw create_or_update_file commit.
        assert len(saved_calls) == 1
        assert saved_calls[0][0].get("name") == "existing-project"


# ---------------------------------------------------------------------------
# VULN (review augmentation): restore snapshot-listing endpoints trusted the
# namespace from the URL path instead of the authenticated project.
#
# The /api/v1/restore/snapshots/{cluster}/{namespace} endpoints validate the
# API key against a caller-supplied project_name but took cluster/namespace
# from the path. The Kopia repository key is derived server-side from that
# namespace, so a tenant holding any valid project key could pass another
# tenant's namespace and enumerate its backup metadata when a shared (single)
# backup bucket is configured. _require_namespace_owned_by_project pins the
# only addressable namespace to get_prefixed_namespace(cluster, project_name).
# ---------------------------------------------------------------------------


class TestRestoreNamespaceOwnership:
    """The endpoint-level guard for path-supplied namespaces."""

    def test_owned_namespace_passes(self) -> None:
        """A project may address its own prefixed namespace."""
        expected = get_prefixed_namespace("local", "my-project")
        # Must not raise.
        _require_namespace_owned_by_project("my-project", "local", expected)

    def test_foreign_namespace_is_rejected(self) -> None:
        """A caller passing another tenant's namespace is rejected with 403."""
        victim_namespace = get_prefixed_namespace("local", "victim-project")
        with pytest.raises(HTTPException) as exc:
            _require_namespace_owned_by_project("attacker-project", "local", victim_namespace)
        assert exc.value.status_code == 403

    def test_unprefixed_namespace_is_rejected(self) -> None:
        """The bare project name (missing cluster prefix) must not match."""
        with pytest.raises(HTTPException) as exc:
            _require_namespace_owned_by_project("my-project", "local", "my-project")
        assert exc.value.status_code == 403

    def test_unknown_cluster_is_rejected(self) -> None:
        """An unknown cluster resolves no prefix and is rejected with 400."""
        with pytest.raises(HTTPException) as exc:
            _require_namespace_owned_by_project("my-project", "does-not-exist", "anything")
        assert exc.value.status_code == 400


class TestRestoreEndpointsEnforceOwnership:
    """The POST restore endpoints require project_name and enforce namespace ownership.

    Before the fix these endpoints had no ``project_name`` parameter at all, so
    ``validate_api_token`` rejected every call with 401 ("Missing project_name
    parameter") -- they were unusable. Now they authenticate like the listing
    endpoints and apply the same namespace-ownership guard, so a valid tenant
    key cannot restore another tenant's backups.
    """

    AUTH: ClassVar[dict[str, str]] = {"X-API-Key": "test-api-key-12345"}

    def test_restore_pvc_foreign_namespace_is_rejected(self, test_client, mock_project_service) -> None:
        """A valid key for test-project may not restore from another tenant's namespace."""
        victim_namespace = get_prefixed_namespace("local", "victim-project")
        response = test_client.post(
            f"/api/v1/restore/pvc/local/{victim_namespace}/app-data?project_name=test-project",
            headers=self.AUTH,
        )
        assert response.status_code == 403

    def test_restore_pvc_missing_project_name_is_unauthorized(self, test_client, mock_project_service) -> None:
        """Without project_name the API key cannot be validated: 401."""
        namespace = get_prefixed_namespace("local", "test-project")
        response = test_client.post(
            f"/api/v1/restore/pvc/local/{namespace}/app-data",
            headers=self.AUTH,
        )
        assert response.status_code == 401

    def test_restore_pvc_owned_namespace_reaches_manager(self, test_client, mock_project_service, monkeypatch) -> None:
        """With a valid key and the project's own namespace the restore is executed."""
        namespace = get_prefixed_namespace("local", "test-project")
        manager = MagicMock()
        manager.restore_pvc = AsyncMock(
            return_value=RestoreResult(
                namespace=namespace,
                pvc_name="app-data",
                success=True,
                target_pvc_name="app-data-restored",
            )
        )
        monkeypatch.setattr("opi.api.restore_router.create_backup_manager", lambda: manager)

        response = test_client.post(
            f"/api/v1/restore/pvc/local/{namespace}/app-data?project_name=test-project",
            headers=self.AUTH,
        )
        assert response.status_code == 200
        manager.restore_pvc.assert_awaited_once()

    def test_restore_database_foreign_namespace_is_rejected(self, test_client, mock_project_service) -> None:
        """Database restore into another tenant's namespace is rejected with 403."""
        victim_namespace = get_prefixed_namespace("local", "victim-project")
        response = test_client.post(
            f"/api/v1/restore/database/local/{victim_namespace}/mydb?project_name=test-project",
            headers=self.AUTH,
            json={
                "target_database_host": "postgresql.svc",
                "target_database_name": "db",
                "target_database_user": "user",
                "target_database_password": "pw",
            },
        )
        assert response.status_code == 403

    def test_restore_bucket_foreign_namespace_is_rejected(self, test_client, mock_project_service) -> None:
        """Bucket restore into another tenant's namespace is rejected with 403."""
        victim_namespace = get_prefixed_namespace("local", "victim-project")
        response = test_client.post(
            f"/api/v1/restore/bucket/local/{victim_namespace}/mybucket?project_name=test-project",
            headers=self.AUTH,
            json={
                "target_minio_endpoint": "http://minio.svc:9000",
                "target_bucket_name": "bucket",
                "target_access_key": "ak",
                "target_secret_key": "sk",
            },
        )
        assert response.status_code == 403
