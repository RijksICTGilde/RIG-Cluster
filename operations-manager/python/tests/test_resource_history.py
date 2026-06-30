"""
Tests for resource history and memory overprovision features.

Covers:
- ProjectFileHandler: history append/read/floor methods
- resource_tuning_service: base component updates, history writing, OOM floor
  enforcement, deepcopy safety in get_project_data
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.resource_tuning_service import (
    get_project_data,
    tune_deployment_resources,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_project_data(
    component_limits="512Mi",
    component_requests="256Mi",
    deployment_limits=None,
    deployment_requests=None,
    component_history=None,
    deployment_history=None,
):
    """Build a minimal project data dict for testing."""
    comp_resources = {
        "requests": {"memory": component_requests, "cpu": "50m"},
        "limits": {"memory": component_limits, "cpu": "1000m"},
    }
    if component_history is not None:
        comp_resources["history"] = component_history

    dep_comp: dict = {"reference": "api"}
    if deployment_limits or deployment_requests or deployment_history is not None:
        dep_comp["resources"] = {}
        if deployment_requests:
            dep_comp["resources"]["requests"] = {"memory": deployment_requests}
        if deployment_limits:
            dep_comp["resources"]["limits"] = {"memory": deployment_limits}
        if deployment_history is not None:
            dep_comp["resources"]["history"] = deployment_history

    return {
        "name": "test-project",
        "components": [{"name": "api", "resources": comp_resources}],
        "deployments": [
            {
                "name": "production",
                "namespace": "test-project",
                "cluster": "odcn-production",
                "components": [dep_comp],
            }
        ],
    }


# ---------------------------------------------------------------------------
# ProjectFileHandler: history methods
# ---------------------------------------------------------------------------


class TestAppendComponentResourceHistory:
    def test_appends_to_empty_history(self):
        handler = ProjectFileHandler()
        data = _make_project_data()
        entry = {"timestamp": "2026-01-01T00:00:00", "limits": {"memory": "256Mi"}, "source": "auto-tune"}

        result = handler.append_component_resource_history(data, "api", entry)

        assert result is True
        history = data["components"][0]["resources"]["history"]
        assert len(history) == 1
        assert history[0]["source"] == "auto-tune"

    def test_inserts_at_front(self):
        handler = ProjectFileHandler()
        old_entry = {"timestamp": "2026-01-01T00:00:00", "source": "old"}
        data = _make_project_data(component_history=[old_entry])
        new_entry = {"timestamp": "2026-01-02T00:00:00", "source": "new"}

        handler.append_component_resource_history(data, "api", new_entry)

        history = data["components"][0]["resources"]["history"]
        assert len(history) == 2
        assert history[0]["source"] == "new"
        assert history[1]["source"] == "old"

    def test_prunes_to_max_entries(self):
        handler = ProjectFileHandler()
        existing = [{"timestamp": f"2026-01-0{i}", "source": f"entry-{i}"} for i in range(5)]
        data = _make_project_data(component_history=existing)
        new_entry = {"timestamp": "2026-02-01", "source": "newest"}

        handler.append_component_resource_history(data, "api", new_entry, max_entries=5)

        history = data["components"][0]["resources"]["history"]
        assert len(history) == 5
        assert history[0]["source"] == "newest"
        assert history[-1]["source"] == "entry-3"  # entry-4 pruned

    def test_returns_false_for_unknown_component(self):
        handler = ProjectFileHandler()
        data = _make_project_data()
        entry = {"timestamp": "2026-01-01", "source": "test"}

        result = handler.append_component_resource_history(data, "nonexistent", entry)
        assert result is False

    def test_creates_resources_dict_if_missing(self):
        handler = ProjectFileHandler()
        data = {"components": [{"name": "api"}]}
        entry = {"timestamp": "2026-01-01", "source": "test"}

        handler.append_component_resource_history(data, "api", entry)

        assert "resources" in data["components"][0]
        assert len(data["components"][0]["resources"]["history"]) == 1


class TestAppendDeploymentComponentResourceHistory:
    def test_appends_to_deployment_component(self):
        handler = ProjectFileHandler()
        data = _make_project_data(deployment_limits="512Mi")
        entry = {"timestamp": "2026-01-01", "source": "auto-tune"}

        result = handler.append_deployment_component_resource_history(data, "production", "api", entry)

        assert result is True
        dep_comp = data["deployments"][0]["components"][0]
        assert len(dep_comp["resources"]["history"]) == 1

    def test_returns_false_for_unknown_deployment(self):
        handler = ProjectFileHandler()
        data = _make_project_data()
        entry = {"timestamp": "2026-01-01", "source": "test"}

        result = handler.append_deployment_component_resource_history(data, "nonexistent", "api", entry)
        assert result is False

    def test_returns_false_for_unknown_component(self):
        handler = ProjectFileHandler()
        data = _make_project_data()
        entry = {"timestamp": "2026-01-01", "source": "test"}

        result = handler.append_deployment_component_resource_history(data, "production", "nonexistent", entry)
        assert result is False

    def test_creates_resources_dict_if_missing(self):
        handler = ProjectFileHandler()
        data = _make_project_data()  # deployment component has no resources
        entry = {"timestamp": "2026-01-01", "source": "test"}

        handler.append_deployment_component_resource_history(data, "production", "api", entry)

        dep_comp = data["deployments"][0]["components"][0]
        assert "resources" in dep_comp
        assert len(dep_comp["resources"]["history"]) == 1


class TestGetResourceHistoryFloor:
    def test_returns_none_when_no_history(self):
        handler = ProjectFileHandler()
        data = _make_project_data()

        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is None

    def test_returns_none_when_no_oom_entries(self):
        handler = ProjectFileHandler()
        history = [{"timestamp": "2026-01-01", "limits": {"memory": "512Mi"}, "source": "auto-tune"}]
        data = _make_project_data(deployment_history=history)

        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is None

    def test_returns_floor_from_deployment_history(self):
        handler = ProjectFileHandler()
        history = [{"timestamp": "2026-01-01", "limits": {"memory": "768Mi"}, "source": "oom-watcher"}]
        data = _make_project_data(deployment_history=history)

        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is not None
        assert floor.floor_mb == 768.0
        assert floor.set_at == "2026-01-01"

    def test_returns_floor_from_component_history(self):
        handler = ProjectFileHandler()
        history = [
            {
                "timestamp": "2026-01-01",
                "limits": {"memory": "1024Mi"},
                "source": "oom-watcher",
                "deployment": "production",
            }
        ]
        data = _make_project_data(component_history=history)

        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is not None
        assert floor.floor_mb == 1024.0

    def test_component_history_from_other_deployment_does_not_count(self):
        """An OOM in another (PR) deployment must not pin this deployment's floor."""
        handler = ProjectFileHandler()
        history = [
            {
                "timestamp": "2026-01-01",
                "limits": {"memory": "1024Mi"},
                "source": "oom-watcher",
                "deployment": "pr746",
            }
        ]
        data = _make_project_data(component_history=history)

        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is None

    def test_component_history_without_deployment_field_does_not_count(self):
        """Legacy def-level entries without deployment attribution are not deployment-scoped."""
        handler = ProjectFileHandler()
        history = [{"timestamp": "2026-01-01", "limits": {"memory": "1024Mi"}, "source": "oom-watcher"}]
        data = _make_project_data(component_history=history)

        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is None

    def test_returns_max_of_both_levels(self):
        handler = ProjectFileHandler()
        comp_history = [
            {
                "timestamp": "2026-01-01",
                "limits": {"memory": "512Mi"},
                "source": "oom-watcher",
                "deployment": "production",
            }
        ]
        dep_history = [{"timestamp": "2026-01-02", "limits": {"memory": "768Mi"}, "source": "oom-watcher"}]
        data = _make_project_data(component_history=comp_history, deployment_history=dep_history)

        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is not None
        assert floor.floor_mb == 768.0
        assert floor.set_at == "2026-01-02"

    def test_only_checks_most_recent_oom_entry(self):
        handler = ProjectFileHandler()
        # Most recent is auto-tune, older is oom-watcher
        dep_history = [
            {"timestamp": "2026-01-02", "limits": {"memory": "256Mi"}, "source": "auto-tune"},
            {"timestamp": "2026-01-01", "limits": {"memory": "768Mi"}, "source": "oom-watcher"},
        ]
        data = _make_project_data(deployment_history=dep_history)

        # Should find the oom-watcher entry even though it's not the most recent
        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is not None
        assert floor.floor_mb == 768.0


# ---------------------------------------------------------------------------
# get_project_data: deepcopy safety
# ---------------------------------------------------------------------------


class TestGetProjectDataDeepCopy:
    @patch("opi.services.resource_tuning_service.get_project_service")
    def test_returns_deep_copy(self, mock_get_service):
        """Mutations to returned data must not affect the cached project.data."""
        original_data = {"name": "test", "components": [{"name": "api", "resources": {}}]}
        mock_project = MagicMock()
        mock_project.data = original_data
        mock_project.filename = "test.yaml"
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        data, _ = get_project_data("test")

        # Mutate the returned copy
        data["components"][0]["resources"]["history"] = [{"source": "injected"}]

        # Original should be unaffected
        assert "history" not in original_data["components"][0]["resources"]

    @patch("opi.services.resource_tuning_service.get_project_service")
    def test_decrypted_fields_not_leaked(self, mock_get_service):
        """If project.data somehow has decrypted fields, they are on the copy, not the source."""
        original_data = {"name": "test", "deployments": [{"name": "prod"}]}
        mock_project = MagicMock()
        mock_project.data = original_data
        mock_project.filename = "test.yaml"
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        data, _ = get_project_data("test")
        data["deployments"][0]["decrypted_configuration"] = {"password": "leaked"}

        # Original must not have the injected key
        assert "decrypted_configuration" not in original_data["deployments"][0]


# ---------------------------------------------------------------------------
# tune_deployment_resources: base component update + history
# ---------------------------------------------------------------------------


def _mock_prometheus_with_usage(max_mb, avg_mb, has_oom=False):
    """Create a mock Prometheus connector returning specific memory values."""
    mock = AsyncMock()
    max_bytes = max_mb * 1024 * 1024
    avg_bytes = avg_mb * 1024 * 1024

    async def custom_query(query):
        if "max_over_time" in query:
            return [{"value": [0, str(max_bytes)]}] if max_mb > 0 else []
        if "avg_over_time" in query:
            return [{"value": [0, str(avg_bytes)]}] if avg_mb > 0 else []
        if "OOMKilled" in query:
            return [{"value": [0, "1"]}] if has_oom else []
        return []

    mock.custom_query.side_effect = custom_query
    return mock


class TestTuneBaseComponentUpdate:
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_base_component_limits_updated_when_increased(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min
    ):
        """When tune raises limits, base component definition should also be raised."""
        data = _make_project_data(component_limits="128Mi", component_requests="64Mi")
        mock_git_connector = AsyncMock()
        mock_git_data.return_value = (data, "test.yaml", mock_git_connector)
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0, has_oom=True)
        mock_reprocess.return_value = True

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        await tune_deployment_resources("test-project", "production")

        # The save receives the modified data - check the base component was updated
        committed_data = mock_pm.save_and_commit_project.call_args[0][0]
        base_limits = committed_data["components"][0]["resources"]["limits"]["memory"]
        assert base_limits == "256Mi"  # 128 * 2.0 (< 256Mi range)

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_history_written_to_both_levels(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min
    ):
        """Tune should write history entries at both deployment and component level."""
        data = _make_project_data(component_limits="128Mi", component_requests="64Mi")
        mock_git_connector = AsyncMock()
        mock_git_data.return_value = (data, "test.yaml", mock_git_connector)
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0, has_oom=True)
        mock_reprocess.return_value = True

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        await tune_deployment_resources("test-project", "production")

        committed_data = mock_pm.save_and_commit_project.call_args[0][0]

        # Deployment-level history
        dep_comp = committed_data["deployments"][0]["components"][0]
        dep_history = dep_comp.get("resources", {}).get("history", [])
        assert len(dep_history) == 1
        assert dep_history[0]["source"] == "oom-watcher"

        # Component-level history (includes deployment name)
        comp_history = committed_data["components"][0]["resources"].get("history", [])
        assert len(comp_history) == 1
        assert comp_history[0]["source"] == "oom-watcher"
        assert comp_history[0]["deployment"] == "production"

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_fresh_oom_floor_holds_limit_but_lowers_request(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min
    ):
        """A recent OOM floor holds the limit, but the request may still drop."""
        oom_history = [
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "limits": {"memory": "512Mi"},
                "source": "oom-watcher",
            }
        ]
        data = _make_project_data(
            component_limits="512Mi",
            component_requests="256Mi",
            deployment_limits="512Mi",
            deployment_requests="256Mi",
            deployment_history=oom_history,
        )
        mock_git_connector = AsyncMock()
        mock_git_data.return_value = (data, "test.yaml", mock_git_connector)
        # Low usage - tuner would normally recommend ~150Mi
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=100, avg_mb=80)
        mock_reprocess.return_value = True

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production")

        # Limit is held at the 512Mi floor, but the request drops to usage+buffer
        assert len(result.changes) == 1
        committed_data = mock_pm.save_and_commit_project.call_args[0][0]
        dep_resources = committed_data["deployments"][0]["components"][0]["resources"]
        assert dep_resources["limits"]["memory"] == "512Mi"
        assert int(dep_resources["requests"]["memory"].removesuffix("Mi")) < 256

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_stale_oom_floor_expires_and_allows_downward_tune(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min
    ):
        """An old OOM floor with usage far below it no longer blocks tuning down."""
        oom_history = [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "limits": {"memory": "512Mi"},
                "source": "oom-watcher",
            }
        ]
        # Coupled limit/request: an expired floor lets the limit follow the
        # request down (a frozen, differing limit would stay put regardless).
        data = _make_project_data(
            component_limits="512Mi",
            component_requests="512Mi",
            deployment_limits="512Mi",
            deployment_requests="512Mi",
            deployment_history=oom_history,
        )
        mock_git_connector = AsyncMock()
        mock_git_data.return_value = (data, "test.yaml", mock_git_connector)
        # Stable low usage, far below the floor (100 < 50% of 512)
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=100, avg_mb=80)
        mock_reprocess.return_value = True

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production")

        assert len(result.changes) == 1
        committed_data = mock_pm.save_and_commit_project.call_args[0][0]
        dep_resources = committed_data["deployments"][0]["components"][0]["resources"]
        assert int(dep_resources["limits"]["memory"].removesuffix("Mi")) < 512


# ---------------------------------------------------------------------------
# kubectl events filtering
# ---------------------------------------------------------------------------


class TestKubectlEventsFiltering:
    @pytest.mark.asyncio
    async def test_filters_old_events(self):
        """Events older than max_age_hours should be excluded."""
        from datetime import datetime, timedelta

        from opi.connectors.kubectl import KubectlConnector

        old_time = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_time = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_output = {
            "items": [
                {
                    "type": "Warning",
                    "reason": "OldEvent",
                    "involvedObject": {"name": "old-pod"},
                    "message": "old event",
                    "metadata": {"creationTimestamp": old_time},
                },
                {
                    "type": "Warning",
                    "reason": "RecentEvent",
                    "involvedObject": {"name": "new-pod"},
                    "message": "recent event",
                    "metadata": {"creationTimestamp": recent_time},
                },
            ]
        }

        import json

        kubectl = KubectlConnector()
        with patch.object(kubectl, "_run_kubectl_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (json.dumps(mock_output), "", 0)

            events = await kubectl.get_namespace_events("test-ns", max_age_hours=2)

        assert len(events) == 1
        assert events[0]["reason"] == "RecentEvent"

    @pytest.mark.asyncio
    async def test_field_selector_for_warning_type(self):
        """Should use field-selector to filter by event type."""
        import json

        from opi.connectors.kubectl import KubectlConnector

        kubectl = KubectlConnector()
        with patch.object(kubectl, "_run_kubectl_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (json.dumps({"items": []}), "", 0)

            await kubectl.get_namespace_events("test-ns", event_type="Warning")

        args = mock_run.call_args[0][0]
        assert "--field-selector" in args
        assert "type=Warning" in args

    @pytest.mark.asyncio
    async def test_no_field_selector_when_type_none(self):
        """Should not add field-selector when event_type is None."""
        import json

        from opi.connectors.kubectl import KubectlConnector

        kubectl = KubectlConnector()
        with patch.object(kubectl, "_run_kubectl_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (json.dumps({"items": []}), "", 0)

            await kubectl.get_namespace_events("test-ns", event_type=None)

        args = mock_run.call_args[0][0]
        assert "--field-selector" not in args
