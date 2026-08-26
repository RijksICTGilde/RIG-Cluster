"""Tests for the resource tuning service (extracted from resource_router)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.resource_tuning_service import (
    TuneResult,
    get_project_data,
    tune_deployment_resources,
)


class TestGetProjectData:
    """Tests for the service-level get_project_data (raises ValueError, not HTTPException)."""

    @patch("opi.services.resource_tuning_service.get_project_store")
    def test_project_not_found(self, mock_get_service):
        mock_service = MagicMock()
        mock_service.get.return_value = None
        mock_get_service.return_value = mock_service

        with pytest.raises(ValueError, match="not found"):
            get_project_data("nonexistent")

    @patch("opi.services.resource_tuning_service.get_project_store")
    def test_project_no_data(self, mock_get_service):
        mock_project = MagicMock()
        mock_project.data = None
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        with pytest.raises(ValueError, match="no data"):
            get_project_data("my-project")

    @patch("opi.services.resource_tuning_service.get_project_store")
    def test_project_found(self, mock_get_service):
        mock_project = MagicMock()
        mock_project.data = {"name": "my-project"}
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        data, filename = get_project_data("my-project")
        assert data["name"] == "my-project"
        assert filename == "my-project.yaml"


class TestTuneDeploymentResources:
    """Tests for tune_deployment_resources service function."""

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_tune_with_oom_kills(
        self,
        mock_get_connector,
        mock_get_from_git,
        mock_pm_cls,
        mock_reprocess,
        mock_prefix,
        mock_max_mem,
        mock_min_mem,
    ):
        """OOM kills should produce a 2x recommendation (128Mi is in the <256Mi range)."""
        project_data = {
            "name": "my-project",
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "64Mi", "cpu": "50m"},
                        "limits": {"memory": "128Mi", "cpu": "1000m"},
                    },
                }
            ],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "odcn-production",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_git_connector = AsyncMock()
        mock_get_from_git.return_value = (project_data, "my-project.yaml")
        # The tune path persists via ProjectManager.save_and_commit_project; mock the
        # constructed manager so no real validation/git runs.
        mock_pm_cls.return_value = AsyncMock()

        mock_connector = AsyncMock()
        mock_connector.custom_query.side_effect = [
            [],  # max: no data
            [],  # avg: no data
            [{"value": [0, "1"]}],  # OOM kill detected
        ]
        mock_get_connector.return_value = mock_connector
        mock_reprocess.return_value = True

        result = await tune_deployment_resources("my-project", "production")

        assert isinstance(result, TuneResult)
        assert len(result.changes) == 1
        assert result.changes[0]["has_oom_kills"] == "True"
        assert result.changes[0]["new_limits_memory"] == "256Mi"  # 128 * 2.0
        assert result.deployment_refresh_triggered is True

    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_no_data_returns_unchanged(self, mock_get_connector, mock_get_from_git, mock_prefix, mock_max_mem):
        """No Prometheus data and no OOM should return unchanged."""
        project_data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "odcn-production",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_git_connector = AsyncMock()
        mock_get_from_git.return_value = (project_data, "my-project.yaml")

        mock_connector = AsyncMock()
        mock_connector.custom_query.return_value = []
        mock_get_connector.return_value = mock_connector

        result = await tune_deployment_resources("my-project")

        assert len(result.changes) == 0
        assert "api" in result.unchanged
        assert result.deployment_refresh_triggered is False

    @pytest.mark.asyncio
    async def test_project_not_found_raises_value_error(self):
        """Should raise ValueError when project doesn't exist."""
        with patch("opi.services.resource_tuning_service.get_project_store") as mock_get_service:
            mock_service = MagicMock()
            mock_service.get.return_value = None
            mock_get_service.return_value = mock_service

            with pytest.raises(ValueError, match="not found"):
                await tune_deployment_resources("nonexistent")

    @pytest.mark.asyncio
    async def test_metrics_unavailable_raises_runtime_error(self):
        """Should raise RuntimeError when metrics backend is unavailable."""
        with (
            patch(
                "opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock
            ) as mock_get_from_git,
            patch(
                "opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock
            ) as mock_get_connector,
        ):
            mock_get_from_git.return_value = ({"deployments": []}, "test.yaml")

            mock_get_connector.side_effect = RuntimeError("Connection refused")

            with pytest.raises(RuntimeError, match="Metrics backend unavailable"):
                await tune_deployment_resources("my-project")


class TestGrowthCeiling:
    """The auto-tune may not grow a component past a multiple of its declared limit.

    Without this bound the only ceiling was the cluster maximum, and asses-k2n/pr-494
    walked from a declared 45Mi to 4096Mi in nine automated rounds.
    """

    @staticmethod
    def _project(root_limit: str, override_limit: str | None, override_request: str | None = None) -> dict:
        deployment_component: dict = {"reference": "api"}
        if override_limit is not None:
            deployment_component["resources"] = {
                "requests": {"memory": override_request or override_limit},
                "limits": {"memory": override_limit},
            }
        return {
            "name": "my-project",
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": root_limit, "cpu": "50m"},
                        "limits": {"memory": root_limit, "cpu": "1000m"},
                    },
                }
            ],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "odcn-production",
                    "components": [deployment_component],
                }
            ],
        }

    @staticmethod
    def _oom_without_metrics() -> AsyncMock:
        """A component that OOMs while Prometheus has no usable data for it.

        This is the fallback that must keep working: an OOM reported by the watcher
        raises the limit even without measurements, using the current limit as the
        baseline. The ceiling bounds it; it must not disable it.
        """
        connector = AsyncMock()
        connector.custom_query.side_effect = [
            [],  # max: no data
            [],  # avg: no data
            [{"value": [0, "1"]}],  # OOM kill detected
        ]
        return connector

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_at_the_ceiling_no_further_increase(
        self, mock_get_connector, mock_get_from_git, mock_pm_cls, mock_reprocess, mock_prefix, mock_max, mock_min
    ):
        """Declared 45Mi, override already at 8x (360Mi): an OOM no longer raises it.

        The override starts with the margin intact (request 64Mi under the limit), so
        the only thing that could produce a change here is the refused bump itself.
        """
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.services.catalog.resource_tuning.config import resource_tuning_config
        from opi.services.resource_tuning_service import describe_growth_ceiling_block

        factor = resource_tuning_config().max_growth_factor
        assert factor == 8.0, "this test is written against a ceiling of 8x"

        project_data = self._project("45Mi", "360Mi", override_request="296Mi")
        mock_get_from_git.return_value = (project_data, "my-project.yaml")
        mock_pm_cls.return_value = AsyncMock()
        mock_get_connector.return_value = self._oom_without_metrics()

        result = await tune_deployment_resources("my-project", "production")

        assert result.changes == [], "at the ceiling the OOM bump must be refused"
        assert "api" in result.unchanged

        # And the message points at manual intervention, naming declared, current and
        # factor -- "could not determine new limits" would send people hunting for
        # missing metrics instead.
        message = describe_growth_ceiling_block(project_data, ProjectFileHandler(), "production", "api")
        assert message is not None
        assert "360Mi" in message
        assert "45Mi" in message
        assert "8x" in message
        assert "by hand" in message

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_below_the_ceiling_still_tunes(
        self, mock_get_connector, mock_get_from_git, mock_pm_cls, mock_reprocess, mock_prefix, mock_max, mock_min
    ):
        """Negative control: well under the ceiling an OOM tunes as before.

        Also pins the anchor: the bump lands on the deployment override, never on the
        catalog component. A ratio bound whose denominator grows along with the
        numerator is no bound at all.
        """
        project_data = self._project("45Mi", "90Mi")
        mock_get_from_git.return_value = (project_data, "my-project.yaml")
        mock_pm_cls.return_value = AsyncMock()
        mock_get_connector.return_value = self._oom_without_metrics()

        result = await tune_deployment_resources("my-project", "production")

        assert len(result.changes) == 1
        new_limit_mi = int(result.changes[0]["new_limits_memory"].removesuffix("Mi"))
        assert 90 < new_limit_mi <= 360, f"must rise, but no further than the 8x ceiling: {new_limit_mi}Mi"

        # The declared root is the anchor and must not move.
        assert project_data["components"][0]["resources"]["limits"]["memory"] == "45Mi"
        assert project_data["components"][0]["resources"]["requests"]["memory"] == "45Mi"

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_the_ceiling_keeps_the_burst_headroom(
        self, mock_get_connector, mock_get_from_git, mock_pm_cls, mock_reprocess, mock_prefix, mock_max, mock_min
    ):
        """Capping the limit at the ceiling must not close the limit/request margin.

        The plain nightly sweep: declared 100Mi (ceiling 800Mi), override 500Mi, a
        component measured at 900Mi. The recommendation lands above the ceiling and is
        capped -- and a cap that also pulls the request up to the capped limit leaves
        headroom 0, the exact burst-death the margin a few lines above forbids.
        """
        from opi.services.catalog.resource_tuning.config import resource_tuning_config

        margin = resource_tuning_config().min_limit_headroom_mi
        project_data = self._project("100Mi", "500Mi")
        mock_get_from_git.return_value = (project_data, "my-project.yaml")
        mock_pm_cls.return_value = AsyncMock()

        connector = AsyncMock()
        connector.custom_query.side_effect = [
            [{"value": [0, str(900 * 1024 * 1024)]}],  # max: 900Mi
            [{"value": [0, str(850 * 1024 * 1024)]}],  # avg: 850Mi
            [],  # no OOM kills
        ]
        mock_get_connector.return_value = connector

        result = await tune_deployment_resources("my-project", "production")

        assert len(result.changes) == 1
        limit_mi = int(result.changes[0]["new_limits_memory"].removesuffix("Mi"))
        request_mi = int(result.changes[0]["new_requests_memory"].removesuffix("Mi"))
        assert limit_mi == 800, f"the ceiling must cap the limit at 8x the declared 100Mi: {limit_mi}Mi"
        assert limit_mi - request_mi >= margin, (
            f"no burst headroom left after the cap: limit {limit_mi}Mi, request {request_mi}Mi"
        )

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_above_the_ceiling_the_request_is_not_lifted_to_the_limit(
        self, mock_get_connector, mock_get_from_git, mock_pm_cls, mock_reprocess, mock_prefix, mock_max, mock_min
    ):
        """A deployment already past the ceiling: the cap must not raise its request.

        For these the working ceiling is the current limit, so an over-ceiling
        recommendation is capped at exactly what is already deployed. Writing the
        request up to that same value is a real change, written to the project file,
        that removes the headroom of precisely the inflated deployments this bound
        exists for.
        """
        from opi.services.catalog.resource_tuning.config import resource_tuning_config

        margin = resource_tuning_config().min_limit_headroom_mi
        project_data = self._project("45Mi", "900Mi", override_request="500Mi")
        mock_get_from_git.return_value = (project_data, "my-project.yaml")
        mock_pm_cls.return_value = AsyncMock()

        connector = AsyncMock()
        connector.custom_query.side_effect = [
            [{"value": [0, str(3000 * 1024 * 1024)]}],  # max: 3000Mi
            [{"value": [0, str(2900 * 1024 * 1024)]}],  # avg: 2900Mi
            [],  # no OOM kills
        ]
        mock_get_connector.return_value = connector

        result = await tune_deployment_resources("my-project", "production")

        assert len(result.changes) == 1
        limit_mi = int(result.changes[0]["new_limits_memory"].removesuffix("Mi"))
        request_mi = int(result.changes[0]["new_requests_memory"].removesuffix("Mi"))
        assert limit_mi == 900, f"capped at the current limit, not raised: {limit_mi}Mi"
        assert limit_mi - request_mi >= margin, (
            f"no burst headroom left after the cap: limit {limit_mi}Mi, request {request_mi}Mi"
        )

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_a_decrease_above_the_ceiling_is_not_blocked(
        self, mock_get_connector, mock_get_from_git, mock_pm_cls, mock_reprocess, mock_prefix, mock_max, mock_min
    ):
        """A ceiling that ignores direction freezes the very cases it should clean up.

        A deployment that already sits at 4096Mi while using ~100Mi must still be
        allowed to come down. Clamping to min(recommendation, ceiling) unconditionally
        would refuse this decrease too and make the inflated value permanent.
        """
        project_data = self._project("45Mi", "4096Mi")
        mock_get_from_git.return_value = (project_data, "my-project.yaml")
        mock_pm_cls.return_value = AsyncMock()

        connector = AsyncMock()
        connector.custom_query.side_effect = [
            [{"value": [0, str(100 * 1024 * 1024)]}],  # max: 100Mi
            [{"value": [0, str(80 * 1024 * 1024)]}],  # avg: 80Mi
            [],  # no OOM kills
        ]
        mock_get_connector.return_value = connector

        result = await tune_deployment_resources("my-project", "production")

        assert len(result.changes) == 1
        new_limit_mi = int(result.changes[0]["new_limits_memory"].removesuffix("Mi"))
        assert new_limit_mi < 4096, "a decrease must survive the ceiling"
