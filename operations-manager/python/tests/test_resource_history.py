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


class TestPruningKeepsOomFloor:
    """Task 5: an auto-tune burst must not evict the OOM floor out of the cap."""

    def test_auto_tune_burst_keeps_newest_oom_watcher_entry(self):
        handler = ProjectFileHandler()
        oom_entry = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "limits": {"memory": "512Mi"},
            "source": "oom-watcher",
        }
        data = _make_project_data(deployment_history=[oom_entry])

        # Six auto-tune entries on top of the single oom-watcher entry.
        for i in range(6):
            handler.append_deployment_component_resource_history(
                data,
                "production",
                "api",
                {
                    "timestamp": f"2026-02-0{i + 1}T00:00:00+00:00",
                    "limits": {"memory": "64Mi"},
                    "requests": {"memory": "48Mi"},
                    "source": "auto-tune",
                },
            )

        history = data["deployments"][0]["components"][0]["resources"]["history"]
        assert any(e["source"] == "oom-watcher" for e in history)
        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is not None
        assert floor.floor_mb == 512


class TestCompactResourceHistory:
    """Task 7: compact windows already filled with identical auto-tune noise."""

    def test_collapses_identical_auto_tune_and_keeps_floor(self):
        # Five identical auto-tune entries (the asses-k2n pr-405..productie run) plus
        # an older oom-watcher entry.
        auto = [
            {
                "timestamp": ts,
                "limits": {"memory": "25Mi"},
                "source": "auto-tune",
                "deployment": "production",
                "reason": "Limit kept equal at 25Mi",
            }
            for ts in (
                "2026-01-05T23:03:05",
                "2026-01-05T23:03:03",
                "2026-01-05T23:03:02",
                "2026-01-05T23:03:00",
                "2026-01-05T23:02:55",
            )
        ]
        oom_entry = {
            "timestamp": "2026-01-01T00:00:00",
            "limits": {"memory": "512Mi"},
            "source": "oom-watcher",
            "deployment": "production",
        }
        data = _make_project_data(deployment_history=[*auto, oom_entry])

        handler = ProjectFileHandler()
        changed = handler.compact_resource_history(data)

        assert changed is True
        new_history = data["deployments"][0]["components"][0]["resources"]["history"]
        assert len([e for e in new_history if e["source"] == "auto-tune"]) == 1
        assert len([e for e in new_history if e["source"] == "oom-watcher"]) == 1
        floor = handler.get_resource_history_floor(data, "production", "api")
        assert floor is not None
        assert floor.floor_mb == 512


# ---------------------------------------------------------------------------
# get_project_data: deepcopy safety
# ---------------------------------------------------------------------------


class TestGetProjectDataDeepCopy:
    @patch("opi.services.resource_tuning_service.get_project_store")
    def test_returns_deep_copy(self, mock_get_service):
        """Mutations to returned data must not affect the cached project.data."""
        original_data = {"name": "test", "components": [{"name": "api", "resources": {}}]}
        mock_project = MagicMock()
        mock_project.data = original_data
        mock_project.filename = "test.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        data, _ = get_project_data("test")

        # Mutate the returned copy
        data["components"][0]["resources"]["history"] = [{"source": "injected"}]

        # Original should be unaffected
        assert "history" not in original_data["components"][0]["resources"]

    @patch("opi.services.resource_tuning_service.get_project_store")
    def test_decrypted_fields_not_leaked(self, mock_get_service):
        """If project.data somehow has decrypted fields, they are on the copy, not the source."""
        original_data = {"name": "test", "deployments": [{"name": "prod"}]}
        mock_project = MagicMock()
        mock_project.data = original_data
        mock_project.filename = "test.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
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
        # OOMKilled EERST. De OOM-query is sinds RC-163 zelf ook een max_over_time
        # (hij moet over een bereik kijken, want de metric bestaat alleen zolang de
        # gestopte pod bestaat), dus "max_over_time in query" onderscheidt de twee
        # niet meer. Op die volgorde gaf deze dubbel de geheugenwaarde terug voor de
        # OOM-vraag, en las de tuner has_oom_kills=True in vijf tests die dat niet
        # bedoelden. De metricnaam is het onderscheid, niet de functie eromheen.
        if "OOMKilled" in query:
            return [{"value": [0, "1"]}] if has_oom else []
        if "max_over_time" in query:
            return [{"value": [0, str(max_bytes)]}] if max_mb > 0 else []
        if "avg_over_time" in query:
            return [{"value": [0, str(avg_bytes)]}] if avg_mb > 0 else []
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
    async def test_root_component_left_untouched_on_oom(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min
    ):
        """Route A: an OOM bump writes only the deployment override, never the root.

        The root is the value the user declared; the tuner no longer ratchets it.
        """
        data = _make_project_data(component_limits="128Mi", component_requests="64Mi")
        mock_git_data.return_value = (data, "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0, has_oom=True)
        mock_reprocess.return_value = True

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        await tune_deployment_resources("test-project", "production")

        committed_data = mock_pm.save_and_commit_project.call_args[0][0]
        # Root component untouched, exactly as declared.
        assert committed_data["components"][0]["resources"]["limits"]["memory"] == "128Mi"
        assert committed_data["components"][0]["resources"]["requests"]["memory"] == "64Mi"
        # The deployment override carries the OOM bump, above the declared root.
        dep_limit = committed_data["deployments"][0]["components"][0]["resources"]["limits"]["memory"]
        assert int(dep_limit.removesuffix("Mi")) > 128

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_history_written_at_deployment_level_only(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min
    ):
        """History is written only at the deployment level (Route A), and records
        both limits and requests (task 6)."""
        data = _make_project_data(component_limits="128Mi", component_requests="64Mi")
        mock_git_data.return_value = (data, "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0, has_oom=True)
        mock_reprocess.return_value = True

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        await tune_deployment_resources("test-project", "production")

        committed_data = mock_pm.save_and_commit_project.call_args[0][0]

        # Deployment-level history, with both limits and requests.
        dep_comp = committed_data["deployments"][0]["components"][0]
        dep_history = dep_comp.get("resources", {}).get("history", [])
        assert len(dep_history) == 1
        assert dep_history[0]["source"] == "oom-watcher"
        assert "memory" in dep_history[0]["limits"]
        assert "memory" in dep_history[0]["requests"]

        # No root-level history is written any more.
        assert "history" not in committed_data["components"][0]["resources"]

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
        # Root request is low (64Mi) so the request may still drop; a high declared
        # root request would floor it (task 4) and defeat this scenario.
        data = _make_project_data(
            component_limits="512Mi",
            component_requests="64Mi",
            deployment_limits="512Mi",
            deployment_requests="256Mi",
            deployment_history=oom_history,
        )
        mock_git_data.return_value = (data, "test.yaml")
        # Low usage - tuner would normally recommend ~150Mi
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=100, avg_mb=80)
        mock_reprocess.return_value = True

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production")

        # Limit is held at the 512Mi floor, but the request drops to usage+buffer
        # (bounded below by the declared root request of 64Mi).
        assert len(result.changes) == 1
        committed_data = mock_pm.save_and_commit_project.call_args[0][0]
        dep_resources = committed_data["deployments"][0]["components"][0]["resources"]
        assert dep_resources["limits"]["memory"] == "512Mi"
        req = int(dep_resources["requests"]["memory"].removesuffix("Mi"))
        assert 64 <= req < 256

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
        # Low declared root (128Mi) so downward tuning has room below the 512Mi
        # override; the declared root is the floor the override cannot cross.
        data = _make_project_data(
            component_limits="128Mi",
            component_requests="128Mi",
            deployment_limits="512Mi",
            deployment_requests="512Mi",
            deployment_history=oom_history,
        )
        mock_git_data.return_value = (data, "test.yaml")
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
        lim = int(dep_resources["limits"]["memory"].removesuffix("Mi"))
        # Tuned down from 512, but never below the declared root (128Mi).
        assert 128 <= lim < 512


def _kubectl_unavailable():
    """A kubectl mock whose deployment reports Available=False (the OOM state)."""
    mock = MagicMock()
    mock.get_deployment_conditions = AsyncMock(
        return_value=[{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}]
    )
    mock.get_vpa_recommendation = AsyncMock(return_value=None)
    return mock


class TestOomPathBypassesAvailabilityGuard:
    """Task 1 + Task 11: the availability guard blocks the nightly sweep but not the
    OOM path. Reproduces the pr-450 field case (45Mi limit, deployment
    Available=False with reason MinimumReplicasUnavailable, no Prometheus data, OOM
    detected)."""

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_nightly_sweep_skips_unavailable_deployment(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        data = _make_project_data(component_limits="45Mi", component_requests="45Mi")
        mock_git_data.return_value = (data, "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0, has_oom=True)
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_unavailable()

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        # No oom_components -> nightly path -> availability guard fires -> skip.
        result = await tune_deployment_resources("test-project", "production")

        assert result.changes == []
        mock_pm.save_and_commit_project.assert_not_called()

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_oom_path_bypasses_guard_and_bumps(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        data = _make_project_data(component_limits="45Mi", component_requests="45Mi")
        mock_git_data.return_value = (data, "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0, has_oom=True)
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_unavailable()

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production", oom_components=["api"])

        assert len(result.changes) == 1
        committed = mock_pm.save_and_commit_project.call_args[0][0]
        # Root component untouched (Route A).
        assert committed["components"][0]["resources"]["limits"]["memory"] == "45Mi"
        dep_res = committed["deployments"][0]["components"][0]["resources"]
        lim = int(dep_res["limits"]["memory"].removesuffix("Mi"))
        req = int(dep_res["requests"]["memory"].removesuffix("Mi"))
        # 3x OOM bump off the 45Mi limit (>= 135Mi), with the request/limit margin held.
        assert lim >= 135
        assert lim >= req + 64
        # Deployment-level oom-watcher history, recording both limits and requests.
        history = dep_res["history"]
        assert history[0]["source"] == "oom-watcher"
        assert "memory" in history[0]["limits"]
        assert "memory" in history[0]["requests"]


class TestLimitRequestMargin:
    """Veldgeval headscale: the written memory limit stays measurably above the
    request even when the measurement is tiny (limit == request kills on the first
    spike)."""

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=10)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_limit_kept_above_request_at_tiny_usage(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        # Measured max 15Mi (headscale), no OOM, low declared root.
        data = _make_project_data(component_limits="25Mi", component_requests="25Mi")
        mock_git_data.return_value = (data, "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=15, avg_mb=15)
        mock_kubectl.isConnected = False  # skip guard + VPA

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production")

        assert len(result.changes) == 1
        dep_res = mock_pm.save_and_commit_project.call_args[0][0]["deployments"][0]["components"][0]["resources"]
        lim = int(dep_res["limits"]["memory"].removesuffix("Mi"))
        req = int(dep_res["requests"]["memory"].removesuffix("Mi"))
        assert lim > req
        assert lim >= req + 64


class TestImplausibleMeasurement:
    """Veldgeval mpfpsm-lcl pr-200: a pod that barely existed inside the window
    measured as a fraction of a Mi, printed as "0Mi", and passed the exact-zero
    test that was supposed to catch exactly this."""

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_fraction_of_a_mi_counts_as_no_data(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        data = _make_project_data(component_limits="1418Mi", component_requests="512Mi")
        mock_git_data.return_value = (data, "test.yaml")
        # 0.3Mi: real enough to survive "== 0", far too small to size on.
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0.3, avg_mb=0.3)
        mock_kubectl.isConnected = False

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production")

        assert result.changes == []
        mock_pm.save_and_commit_project.assert_not_called()


def _starved_project_data(disabled_reason=None):
    """Project with a 25Mi override under a 1418Mi root, optionally auto-disabled."""
    data = _make_project_data(
        component_limits="1418Mi",
        component_requests="512Mi",
        deployment_limits="25Mi",
        deployment_requests="25Mi",
    )
    if disabled_reason is not None:
        dep_comp = data["deployments"][0]["components"][0]
        dep_comp["disabled"] = True
        dep_comp["disabled-reason"] = disabled_reason
    return data


class TestRootRepair:
    """Veldgeval mpfpsm-lcl pr-200: an override written below the declared root
    starves the component, and the starvation then blocks both routes that would
    correct it (no pod to measure, and Available=False for the guard)."""

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_override_below_root_restored_without_data(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        mock_git_data.return_value = (_starved_project_data(), "test.yaml")
        # Nothing to measure, and the deployment reports Available=False.
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0)
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_unavailable()

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production")

        assert len(result.changes) == 1
        assert result.changes[0]["source"] == "root"
        dep_res = mock_pm.save_and_commit_project.call_args[0][0]["deployments"][0]["components"][0]["resources"]
        assert dep_res["limits"]["memory"] == "1418Mi"
        assert dep_res["requests"]["memory"] == "512Mi"

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_repair_clears_an_oom_disable(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        """A component sanitize switched off for OOM comes back on with its memory."""
        mock_git_data.return_value = (_starved_project_data("12 restarts; OOMKilled detected"), "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0)
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_unavailable()

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        await tune_deployment_resources("test-project", "production")

        dep_comp = mock_pm.save_and_commit_project.call_args[0][0]["deployments"][0]["components"][0]
        assert dep_comp["disabled"] is False
        assert "disabled-reason" not in dep_comp
        assert dep_comp["resources"]["limits"]["memory"] == "1418Mi"

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_repair_leaves_an_image_pull_disable_alone(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        """Memory says nothing about a missing image: that disable stays."""
        mock_git_data.return_value = (_starved_project_data("ImagePullBackOff: manifest unknown"), "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0)
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_unavailable()

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        await tune_deployment_resources("test-project", "production")

        dep_comp = mock_pm.save_and_commit_project.call_args[0][0]["deployments"][0]["components"][0]
        assert dep_comp["disabled"] is True
        assert dep_comp["disabled-reason"] == "ImagePullBackOff: manifest unknown"

    @patch("opi.services.resource_tuning_service.supports_vpa", return_value=False)
    @patch("opi.services.resource_tuning_service.KubectlConnector")
    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_override_above_root_is_left_to_the_measurement(
        self, mock_connector, mock_git_data, mock_pm_cls, mock_reprocess, mock_prefix, mock_min, mock_kubectl, mock_vpa
    ):
        """The root is a floor, not a target: an override above it is not pulled down
        by the repair, and an unavailable deployment is still skipped."""
        data = _make_project_data(
            component_limits="128Mi",
            component_requests="64Mi",
            deployment_limits="512Mi",
            deployment_requests="256Mi",
        )
        mock_git_data.return_value = (data, "test.yaml")
        mock_connector.return_value = _mock_prometheus_with_usage(max_mb=0, avg_mb=0)
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_unavailable()

        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await tune_deployment_resources("test-project", "production")

        assert result.changes == []
        mock_pm.save_and_commit_project.assert_not_called()


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
