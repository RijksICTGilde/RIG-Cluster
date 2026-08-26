"""Tests for opi.manager.project_manager module.

Focuses on: async correctness, command construction, edge cases in deployment processing.
"""

import inspect

import pytest
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager


class TestLooksLikeRenderFailure:
    """The manifests-endpoint body classifier that decides whether to block the deploy."""

    def test_generation_error_is_render_failure(self):
        from opi.manager.project_manager import _looks_like_render_failure

        body = (
            "Failed to load target state: failed to generate manifests in 'x': rpc error: "
            "code = Unknown desc = kustomize build failed exit status 1: may not add resource ..."
        )
        assert _looks_like_render_failure(body) is True

    def test_real_manifests_endpoint_body_is_render_failure(self):
        # Exact shape from a live sandbox 500 manifests-endpoint response.
        from opi.manager.project_manager import _looks_like_render_failure

        body = (
            '{"error":"plugin sidecar failed. error generating manifests in cmp: rpc error: '
            "code = Unknown desc = error generating manifests: `/bin/bash -c ...` failed exit status 1: "
            "ERROR: Namespace 'rig-x' does not exist\"}"
        )
        assert _looks_like_render_failure(body) is True

    def test_auth_or_network_error_is_not_render_failure(self):
        from opi.manager.project_manager import _looks_like_render_failure

        assert _looks_like_render_failure("401 Unauthorized") is False
        assert _looks_like_render_failure("connection refused") is False
        assert _looks_like_render_failure("") is False
        assert _looks_like_render_failure(None) is False


class TestAsyncCorrectness:
    """All calls to async functions must use await - missing await silently returns a coroutine object."""

    def test_decrypt_age_content_calls_are_awaited(self):
        """Every call to decrypt_age_content in project_manager must be awaited.

        Missing await causes the coroutine to be stringified as '<coroutine object ...>'
        instead of the actual decrypted value.
        """
        source = inspect.getsource(ProjectManager)

        # Find all lines with decrypt_age_content calls
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "decrypt_age_content(" in stripped and not stripped.startswith("#"):
                # Skip import lines
                if "import" in stripped:
                    continue
                assert "await" in stripped, (
                    f"Line {i}: decrypt_age_content() is async but called without await: {stripped}"
                )


class TestResolveDeploymentFilter:
    """The single/plural deployment-filter normalizer underpins scoped redeploys."""

    def test_none_when_both_absent(self):
        from opi.manager.project_manager import _resolve_deployment_filter

        assert _resolve_deployment_filter(None, None) is None

    def test_single_name_wraps_to_list(self):
        from opi.manager.project_manager import _resolve_deployment_filter

        assert _resolve_deployment_filter("dev", None) == ["dev"]

    def test_list_takes_precedence_over_single(self):
        from opi.manager.project_manager import _resolve_deployment_filter

        assert _resolve_deployment_filter("dev", ["a", "b"]) == ["a", "b"]

    def test_empty_list_preserved_not_collapsed_to_none(self):
        """Empty list means 'zero deployments', must not become None ('all')."""
        from opi.manager.project_manager import _resolve_deployment_filter

        assert _resolve_deployment_filter("dev", []) == []


class TestGetDeploymentsListFilter:
    """get_deployments honors an explicit list, with empty = zero (not all)."""

    @staticmethod
    def _deps():
        from opi.core.config import settings

        return [{"name": n, "namespace": "p", "cluster": settings.CLUSTER_MANAGER} for n in ("a", "b", "c")]

    @pytest.mark.asyncio
    async def test_list_filters_to_subset(self):
        from unittest.mock import AsyncMock, patch

        pm = ProjectManager.__new__(ProjectManager)
        with patch.object(
            ProjectManager, "get_contents", new=AsyncMock(return_value={"name": "p", "deployments": self._deps()})
        ):
            result = await pm.get_deployments(deployment_names=["a", "c"])
        assert [d["name"] for d in result] == ["a", "c"]

    @pytest.mark.asyncio
    async def test_empty_list_yields_zero(self):
        from unittest.mock import AsyncMock, patch

        pm = ProjectManager.__new__(ProjectManager)
        with patch.object(
            ProjectManager, "get_contents", new=AsyncMock(return_value={"name": "p", "deployments": self._deps()})
        ):
            result = await pm.get_deployments(deployment_names=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_none_yields_all(self):
        from unittest.mock import AsyncMock, patch

        pm = ProjectManager.__new__(ProjectManager)
        with patch.object(
            ProjectManager, "get_contents", new=AsyncMock(return_value={"name": "p", "deployments": self._deps()})
        ):
            result = await pm.get_deployments()
        assert [d["name"] for d in result] == ["a", "b", "c"]


class TestMissingFStrings:
    """Strings with {var} placeholders must be f-strings, otherwise the variable is not interpolated."""

    def test_storage_type_error_includes_actual_type(self):
        """ValueError for unknown storage type must include the actual type value, not literal '{storage_type}'."""
        pm = ProjectManager.__new__(ProjectManager)
        with pytest.raises(ValueError, match="bogus_type"):
            pm._generate_storage_env_vars_from_services([{"mount-path": "/data", "type": "bogus_type"}])

    def test_no_deployments_warning_includes_project_name(self):
        """Log strings with {var} placeholders must use f-string prefix to interpolate variables."""
        source = inspect.getsource(ProjectManager.check_and_create_sops_secrets_in_namespaces)
        # Find the warning log line about no deployments
        for line in source.split("\n"):
            stripped = line.strip()
            if "No deployments found in project" in stripped and "logger" in stripped:
                # The string should be an f-string so {project_name} gets interpolated
                assert 'f"' in stripped or "f'" in stripped, (
                    f"Missing f-prefix on string with {{project_name}} placeholder: {stripped}"
                )


class TestServiceCategoryMapping:
    """Regression tests for _get_service_category_name.

    The category map is consumed by alias categorisation
    (project_manager._categorize_alias). Because RedisVariables / DatabaseVariables
    are shared between the regular and NAMESPACE_* service definitions, the
    var_to_service lookup ends up keyed by whichever variant comes last in
    SERVICE_DEFINITIONS dict iteration order. If both variants don't collapse
    to the same category name, aliases referencing those vars silently route
    to a bucket no project consumes (e.g. "namespace-redis") and are dropped.
    """

    def test_redis_and_namespace_redis_collapse_to_redis_category(self):
        from opi.manager.project_manager import ProjectManager
        from opi.services.services_enums import ServiceType

        m = ProjectManager.__new__(ProjectManager)  # bypass __init__
        assert m._get_service_category_name(ServiceType.REDIS) == "redis"
        assert m._get_service_category_name(ServiceType.NAMESPACE_REDIS) == "redis"

    def test_postgresql_and_namespace_postgresql_collapse_to_database_category(self):
        from opi.manager.project_manager import ProjectManager
        from opi.services.services_enums import ServiceType

        m = ProjectManager.__new__(ProjectManager)
        assert m._get_service_category_name(ServiceType.POSTGRESQL_DATABASE) == "database"
        assert m._get_service_category_name(ServiceType.NAMESPACE_POSTGRESQL_DATABASE) == "database"


def _valid_project_for_save() -> dict:
    """A minimal schema-valid project used by the central-save tests."""
    return {
        "name": "valid-project",
        "description": "A valid project",
        "clusters": [settings.CLUSTER_MANAGER],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "repositories": [
            {
                "name": "main-repo",
                "url": "ssh://git@host.docker.internal:2222/srv/git/valid.git",
                "branch": "main",
                "path": "infra",
            }
        ],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "ports": {"inbound": [8080], "outbound": [443]},
                # No v1 `storage:` block: that form only lives in the v1 schema now
                # (RC-32), and this fixture is meant to be valid at the latest version.
                "services": [
                    {
                        "reference": "persistent-storage",
                        "config": [{"name": "data", "mount-path": "/data", "size": "10Gi"}],
                    }
                ],
            }
        ],
        "deployments": [
            {
                "name": "productie",
                # Het cluster van deze test-OPI, niet een vaste naam. save_and_commit_project
                # weigert sinds de eigendomsgrendel een projectbestand waarvan geen enkele
                # deployment op CLUSTER_MANAGER draait, en dat is precies de bedoeling.
                # Regel 100 in dit bestand deed dit al zo.
                "cluster": settings.CLUSTER_MANAGER,
                "namespace": "valid-project",
                "repository": "main-repo",
                "components": [{"reference": "frontend", "image": "nginx:latest"}],
            }
        ],
    }


class TestSaveAndCommitProjectValidation:
    """save_and_commit_project must validate schema + structural integrity BEFORE any write/commit.

    The persist path now runs inside GitProjectStore, so these tests drive a real
    store wired to a fake git connector: the validate-before-write ordering, the
    single commit, and the cache write-through are all exercised for real rather
    than asserted against a mock of the thing under test.
    """

    @staticmethod
    def _store_with(git, tmp_path):
        """A real GitProjectStore whose only fake part is the git connector."""
        from opi.services.project_store import GitProjectStore

        store = GitProjectStore(working_dir=str(tmp_path))

        async def _get_connector():
            return git

        store.get_connector = _get_connector
        return store

    @staticmethod
    def _fake_git():
        from unittest.mock import AsyncMock

        git = AsyncMock()
        # No prior committed version: `before` is None and nothing is migrated.
        git.show_file_at = AsyncMock(return_value=None)
        git.get_local_commit_hash = AsyncMock(return_value="deadbeef")
        # The store builds a commit from git objects instead of writing into a working
        # tree, so build_commit is the thing that must (not) happen.
        git.build_commit = AsyncMock(return_value="newcommit")
        return git

    @pytest.mark.asyncio
    async def test_schema_invalid_dict_raises_and_never_commits(self, tmp_path):
        """A schema violation must abort before the file write and the commit run."""
        from unittest.mock import patch

        from opi.core.project_schema import ProjectSchemaError

        pm = ProjectManager.__new__(ProjectManager)
        pm._project_file_relative_path = "projects/valid-project.yaml"

        git = self._fake_git()
        invalid = _valid_project_for_save()
        invalid["name"] = 123  # schema requires a string

        with (
            patch("opi.manager.project_manager.get_project_store", return_value=self._store_with(git, tmp_path)),
            patch("opi.services.project_store.get_project_service") as svc_mock,
            pytest.raises(ProjectSchemaError),
        ):
            await pm.save_and_commit_project(invalid, "msg")

        git.build_commit.assert_not_awaited()
        git.push_changes.assert_not_awaited()
        svc_mock.return_value.load_project_from_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_structural_dangling_reference_raises(self):
        """A deployment referencing an undefined component is a structural rejection."""
        from opi.core.project_schema import ProjectIntegrityError

        pm = ProjectManager.__new__(ProjectManager)
        project = _valid_project_for_save()
        project["deployments"][0]["components"] = [{"reference": "ghost", "image": "nginx:latest"}]

        with pytest.raises(ProjectIntegrityError):
            await pm._validate_structural_integrity(project)

    @pytest.mark.asyncio
    async def test_structural_duplicate_component_raises(self):
        """Two components with the same name is a structural rejection."""
        from opi.core.project_schema import ProjectIntegrityError

        pm = ProjectManager.__new__(ProjectManager)
        project = _valid_project_for_save()
        project["components"].append({"name": "frontend", "type": "deployment"})

        with pytest.raises(ProjectIntegrityError):
            await pm._validate_structural_integrity(project)

    @pytest.mark.asyncio
    async def test_valid_dict_commits_once_and_refreshes_cache(self, tmp_path):
        """A valid dict is written once, committed once, and refreshes the read-only cache."""
        from unittest.mock import AsyncMock, patch

        pm = ProjectManager.__new__(ProjectManager)
        pm._project_file_relative_path = "projects/valid-project.yaml"

        git = self._fake_git()
        valid = _valid_project_for_save()

        with (
            patch("opi.services.project_store.validate_project_structure", new=AsyncMock()),
            patch("opi.manager.project_manager.get_project_store", return_value=self._store_with(git, tmp_path)),
            patch("opi.services.project_store.get_project_service") as svc_mock,
        ):
            await pm.save_and_commit_project(valid, "Add component")

        git.build_commit.assert_awaited_once()
        assert git.build_commit.await_args.args[1] == "Add component"
        git.push_changes.assert_awaited_once()
        svc_mock.return_value.load_project_from_data.assert_called_once()

    @pytest.mark.asyncio
    async def test_validation_happens_before_the_commit_is_built(self, tmp_path):
        """Ordering guard: nothing is committed until validation has passed.

        The write and the commit are one operation now (the content goes straight into a
        git object), so the ordering that still carries meaning is validate -> build ->
        push: an invalid project must never reach an object, let alone the remote.
        """
        from unittest.mock import AsyncMock, patch

        pm = ProjectManager.__new__(ProjectManager)
        pm._project_file_relative_path = "projects/valid-project.yaml"

        order: list[str] = []
        git = self._fake_git()
        git.build_commit = AsyncMock(side_effect=lambda *a, **k: (order.append("build"), "newcommit")[1])
        git.push_changes = AsyncMock(side_effect=lambda *a, **k: order.append("push"))
        valid = _valid_project_for_save()

        with (
            patch(
                "opi.services.project_store.validate_project_structure",
                new=AsyncMock(side_effect=lambda *a, **k: order.append("validate")),
            ),
            patch("opi.manager.project_manager.get_project_store", return_value=self._store_with(git, tmp_path)),
            patch("opi.services.project_store.get_project_service"),
        ):
            await pm.save_and_commit_project(valid, "Add component")

        assert order == ["validate", "build", "push"]

    @pytest.mark.asyncio
    async def test_refresh_cache_false_skips_cache_load(self, tmp_path):
        """refresh_cache=False still commits but does not touch the in-memory cache."""
        from unittest.mock import AsyncMock, patch

        pm = ProjectManager.__new__(ProjectManager)
        pm._project_file_relative_path = "projects/valid-project.yaml"

        git = self._fake_git()
        valid = _valid_project_for_save()

        with (
            patch("opi.services.project_store.validate_project_structure", new=AsyncMock()),
            patch("opi.manager.project_manager.get_project_store", return_value=self._store_with(git, tmp_path)),
            patch("opi.services.project_store.get_project_service") as svc_mock,
        ):
            await pm.save_and_commit_project(valid, "Add component", refresh_cache=False)

        git.build_commit.assert_awaited_once()
        svc_mock.return_value.load_project_from_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_enforce_validation_false_commits_despite_structural_failure(self, tmp_path):
        """Trusted background mutators (enforce_validation=False) persist even when the
        project has pre-existing structural drift; validation runs but only warns."""
        from unittest.mock import AsyncMock, patch

        from opi.core.project_schema import ProjectIntegrityError

        pm = ProjectManager.__new__(ProjectManager)
        pm._project_file_relative_path = "projects/valid-project.yaml"

        git = self._fake_git()
        valid = _valid_project_for_save()

        with (
            patch(
                "opi.services.project_store.validate_project_structure",
                new=AsyncMock(side_effect=ProjectIntegrityError("pre-existing drift")),
            ),
            patch("opi.manager.project_manager.get_project_store", return_value=self._store_with(git, tmp_path)),
            patch("opi.services.project_store.get_project_service"),
        ):
            await pm.save_and_commit_project(valid, "auto-tune", enforce_validation=False)

        git.build_commit.assert_awaited_once()
        git.push_changes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_enforce_validation_true_aborts_on_structural_failure(self, tmp_path):
        """A user-driven edit that introduces drift is rejected before any write."""
        from unittest.mock import AsyncMock, patch

        from opi.core.project_schema import ProjectIntegrityError

        pm = ProjectManager.__new__(ProjectManager)
        pm._project_file_relative_path = "projects/valid-project.yaml"

        git = self._fake_git()
        valid = _valid_project_for_save()

        with (
            patch(
                "opi.services.project_store.validate_project_structure",
                new=AsyncMock(side_effect=ProjectIntegrityError("introduced drift")),
            ),
            patch("opi.manager.project_manager.get_project_store", return_value=self._store_with(git, tmp_path)),
            patch("opi.services.project_store.get_project_service"),
            pytest.raises(ProjectIntegrityError),
        ):
            await pm.save_and_commit_project(valid, "user edit")

        git.build_commit.assert_not_awaited()
        git.push_changes.assert_not_awaited()
