"""Tests for the OOM watcher fire-and-forget service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.oom_watcher import (
    _check_oom_kills_via_kubectl,
    _run_oom_check,
    schedule_oom_check,
)

# ---------------------------------------------------------------------------
# _check_oom_kills_via_kubectl
# ---------------------------------------------------------------------------


class TestCheckOomKillsViaKubectl:
    """Tests for kubectl-based OOM detection."""

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_no_oom_detected(self, mock_kubectl_cls):
        """No OOM kills returns False."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True

        pods_json = '{"items": [{"status": {"containerStatuses": [{"name": "app", "lastState": {}}]}}]}'
        mock_kubectl.run_command = AsyncMock(return_value=(pods_json, "", 0))

        result = await _check_oom_kills_via_kubectl("rig-prd-ns", "prod-api")
        assert result is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_oom_detected(self, mock_kubectl_cls):
        """OOM kill in lastState.terminated returns True."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True

        pods_json = (
            '{"items": [{"metadata": {"name": "prod-api-abc"}, '
            '"status": {"containerStatuses": [{"name": "app", '
            '"lastState": {"terminated": {"reason": "OOMKilled"}}}]}}]}'
        )
        mock_kubectl.run_command = AsyncMock(return_value=(pods_json, "", 0))

        result = await _check_oom_kills_via_kubectl("rig-prd-ns", "prod-api")
        assert result is True

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_kubectl_not_connected(self, mock_kubectl_cls):
        """When kubectl is not connected, returns False gracefully."""
        mock_kubectl_cls.isConnected = False

        result = await _check_oom_kills_via_kubectl("rig-prd-ns", "prod-api")
        assert result is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_kubectl_command_fails(self, mock_kubectl_cls):
        """When kubectl command fails, returns False gracefully."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True

        mock_kubectl.run_command = AsyncMock(return_value=("", "error", 1))

        result = await _check_oom_kills_via_kubectl("rig-prd-ns", "prod-api")
        assert result is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_no_pods_found(self, mock_kubectl_cls):
        """When no pods are returned, returns False."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True

        mock_kubectl.run_command = AsyncMock(return_value=('{"items": []}', "", 0))

        result = await _check_oom_kills_via_kubectl("rig-prd-ns", "prod-api")
        assert result is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_terminated_non_oom_reason(self, mock_kubectl_cls):
        """Terminated with a non-OOM reason returns False."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True

        pods_json = (
            '{"items": [{"metadata": {"name": "prod-api-abc"}, '
            '"status": {"containerStatuses": [{"name": "app", '
            '"lastState": {"terminated": {"reason": "Error"}}}]}}]}'
        )
        mock_kubectl.run_command = AsyncMock(return_value=(pods_json, "", 0))

        result = await _check_oom_kills_via_kubectl("rig-prd-ns", "prod-api")
        assert result is False


# ---------------------------------------------------------------------------
# _run_oom_check
# ---------------------------------------------------------------------------


class TestRunOomCheck:
    """Tests for the internal OOM check coroutine."""

    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_no_oom_does_not_tune(self, mock_prefix, mock_get_data, mock_check_oom, mock_tune):
        """When no OOM is detected, tune should not be called."""
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )
        mock_check_oom.return_value = False

        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_tune.assert_not_called()

    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_oom_detected_triggers_tune(self, mock_prefix, mock_get_data, mock_check_oom, mock_tune):
        """When OOM is detected, tune should be called."""
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )
        mock_check_oom.return_value = True
        mock_tune.return_value = MagicMock(changes=[{"component": "api"}])

        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_tune.assert_called_once_with("myproject", "production")

    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @pytest.mark.asyncio
    async def test_project_not_found_graceful(self, mock_get_data, mock_check_oom, mock_tune):
        """When project is not found, should log and return without crashing."""
        mock_get_data.side_effect = ValueError("Project not found")

        # Should not raise
        await _run_oom_check("missing-project", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_check_oom.assert_not_called()
        mock_tune.assert_not_called()

    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_tune_failure_does_not_crash(self, mock_prefix, mock_get_data, mock_check_oom, mock_tune):
        """When tune raises an exception, the watcher should not crash."""
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )
        mock_check_oom.return_value = True
        mock_tune.side_effect = RuntimeError("Metrics unavailable")

        # Should not raise
        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)


# ---------------------------------------------------------------------------
# schedule_oom_check
# ---------------------------------------------------------------------------


class TestScheduleOomCheck:
    """Tests for the public schedule_oom_check function."""

    @patch("opi.services.oom_watcher.settings")
    def test_disabled_returns_none(self, mock_settings):
        """When OOM_WATCHER_ENABLED is False, returns None."""
        mock_settings.OOM_WATCHER_ENABLED = False

        result = schedule_oom_check("myproject", "production")
        assert result is None

    @patch("opi.services.oom_watcher.settings")
    def test_max_attempts_exceeded_returns_none(self, mock_settings):
        """When attempt > max_attempts, returns None."""
        mock_settings.OOM_WATCHER_ENABLED = True
        mock_settings.OOM_WATCHER_DELAY_SECONDS = 10
        mock_settings.OOM_WATCHER_MAX_ATTEMPTS = 3

        result = schedule_oom_check("myproject", "production", attempt=4)
        assert result is None

    @patch("opi.services.oom_watcher.settings")
    @pytest.mark.asyncio
    async def test_schedules_task(self, mock_settings):
        """When enabled and within attempts, schedules an asyncio task."""
        mock_settings.OOM_WATCHER_ENABLED = True
        mock_settings.OOM_WATCHER_DELAY_SECONDS = 0
        mock_settings.OOM_WATCHER_MAX_ATTEMPTS = 3

        with patch("opi.services.oom_watcher._run_oom_check", new_callable=AsyncMock) as mock_run:
            task = schedule_oom_check("myproject", "production", delay_seconds=0, attempt=1)
            assert task is not None
            assert isinstance(task, asyncio.Task)

            # Let the task complete
            await task

            mock_run.assert_called_once_with("myproject", "production", 1, 3, 0)

    @patch("opi.services.oom_watcher.settings")
    @pytest.mark.asyncio
    async def test_attempt_at_max_still_runs(self, mock_settings):
        """Attempt equal to max_attempts should still run (only > max is blocked)."""
        mock_settings.OOM_WATCHER_ENABLED = True
        mock_settings.OOM_WATCHER_DELAY_SECONDS = 0
        mock_settings.OOM_WATCHER_MAX_ATTEMPTS = 3

        with patch("opi.services.oom_watcher._run_oom_check", new_callable=AsyncMock) as mock_run:
            task = schedule_oom_check("myproject", "production", delay_seconds=0, attempt=3)
            assert task is not None
            await task
            mock_run.assert_called_once()
