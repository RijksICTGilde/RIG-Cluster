"""Tests for the OOM watcher fire-and-forget service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.oom_watcher import (
    OOMDetectedError,
    _check_oom_kills_via_kubectl,
    _run_oom_check,
    create_oom_progressing_callback,
    detect_oom_kills,
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


# ---------------------------------------------------------------------------
# detect_oom_kills
# ---------------------------------------------------------------------------


class TestDetectOomKills:
    """Tests for the multi-component OOM detection function."""

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_returns_oom_components(self, mock_check):
        """Should return only the components that have OOM kills."""
        mock_check.side_effect = [True, False, True]

        result = await detect_oom_kills("rig-prd-ns", ["comp-a", "comp-b", "comp-c"])

        assert result == ["comp-a", "comp-c"]
        assert mock_check.call_count == 3

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_oom(self, mock_check):
        """Should return empty list when no OOM kills detected."""
        mock_check.return_value = False

        result = await detect_oom_kills("rig-prd-ns", ["comp-a", "comp-b"])

        assert result == []


# ---------------------------------------------------------------------------
# create_oom_progressing_callback
# ---------------------------------------------------------------------------


class TestCreateOomProgressingCallback:
    """Tests for the on_progressing callback factory."""

    def setup_method(self):
        """Reset inline OOM attempts between tests."""
        from opi.services.oom_watcher import _inline_oom_attempts

        _inline_oom_attempts.clear()

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_skips_before_grace_period(self, mock_check):
        """Should not check for OOM before the grace period."""
        callback = create_oom_progressing_callback(
            "myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=30
        )

        await callback(10)
        await callback(20)

        mock_check.assert_not_called()

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_raises_on_oom_after_grace(self, mock_check):
        """Should raise OOMDetectedError when OOM is detected after grace period."""
        mock_check.return_value = True
        callback = create_oom_progressing_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        with pytest.raises(OOMDetectedError) as exc_info:
            await callback(10)

        assert "comp-a" in str(exc_info.value)
        assert exc_info.value.components == ["comp-a"]
        assert exc_info.value.namespace == "rig-prd-ns"

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_no_raise_when_no_oom(self, mock_check):
        """Should not raise when no OOM is detected after grace period."""
        mock_check.return_value = False
        callback = create_oom_progressing_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        # Should not raise
        await callback(10)

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_checks_every_poll_after_grace(self, mock_check):
        """Should check on every poll iteration after grace period."""
        mock_check.return_value = False
        callback = create_oom_progressing_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        await callback(10)  # check
        await callback(15)  # check
        await callback(20)  # check

        assert mock_check.call_count == 3

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_stops_after_max_elapsed(self, mock_check):
        """Should stop checking after OOM_CHECK_MAX_ELAPSED_SECONDS."""
        mock_check.return_value = False
        callback = create_oom_progressing_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        await callback(10)  # check
        await callback(130)  # > 120s max, should skip

        mock_check.assert_called_once()

    def test_returns_none_when_max_attempts_reached(self):
        """Should return None when max inline OOM attempts have been exhausted."""
        from opi.services.oom_watcher import OOM_INLINE_MAX_ATTEMPTS, _inline_oom_attempts

        _inline_oom_attempts["myproject/production"] = OOM_INLINE_MAX_ATTEMPTS

        result = create_oom_progressing_callback("myproject", "production", "rig-prd-ns", ["comp-a"])
        assert result is None

    @patch("opi.services.oom_watcher._check_oom_kills_via_kubectl", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_increments_attempt_on_oom(self, mock_check):
        """Should increment the attempt counter when OOM is detected."""
        from opi.services.oom_watcher import _inline_oom_attempts

        mock_check.return_value = True
        callback = create_oom_progressing_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(OOMDetectedError):
            await callback(5)

        assert _inline_oom_attempts["myproject/production"] == 1

    def test_resets_counter_on_creation(self):
        """Should reset attempt counter when a new callback is created (fresh deploy)."""
        from opi.services.oom_watcher import _inline_oom_attempts

        _inline_oom_attempts["myproject/production"] = 2

        create_oom_progressing_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        assert "myproject/production" not in _inline_oom_attempts
