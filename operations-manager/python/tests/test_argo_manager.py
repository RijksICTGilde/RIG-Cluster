"""Tests for ArgoManager.wait_for_application_synced."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.argo_manager import ArgoManager


def _make_status(sync: str, health: str, operation_phase: str | None = None, operation_message: str = "") -> dict:
    """Build a minimal ArgoCD application status dict."""
    status: dict = {
        "status": {
            "sync": {"status": sync},
            "health": {"status": health},
        }
    }
    if operation_phase:
        status["status"]["operationState"] = {
            "phase": operation_phase,
            "message": operation_message,
        }
    return status


@pytest.fixture
def argo_manager() -> ArgoManager:
    project_manager = MagicMock()
    return ArgoManager(project_manager)


@pytest.fixture
def mock_connector() -> AsyncMock:
    connector = AsyncMock()
    connector.login = AsyncMock(return_value=True)
    return connector


class TestWaitForApplicationSynced:
    """Tests for ArgoManager.wait_for_application_synced."""

    @pytest.mark.asyncio
    async def test_immediately_synced_and_healthy(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should return True when app is already synced and healthy."""
        mock_connector.get_application_status = AsyncMock(return_value=_make_status("Synced", "Healthy"))

        with patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector):
            result = await argo_manager.wait_for_application_synced("my-app", timeout=10, poll_interval=1)

        assert result is True
        mock_connector.get_application_status.assert_called_once_with("my-app")

    @pytest.mark.asyncio
    async def test_becomes_synced_after_polling(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should return True after a few polls when app transitions to synced+healthy."""
        mock_connector.get_application_status = AsyncMock(
            side_effect=[
                _make_status("OutOfSync", "Progressing"),
                _make_status("Synced", "Progressing"),
                _make_status("Synced", "Healthy"),
            ]
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await argo_manager.wait_for_application_synced("my-app", timeout=30, poll_interval=2)

        assert result is True
        assert mock_connector.get_application_status.call_count == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_failed_raises_runtime_error(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should raise RuntimeError when operationState.phase is Failed."""
        mock_connector.get_application_status = AsyncMock(
            return_value=_make_status(
                "OutOfSync", "Progressing", operation_phase="Failed", operation_message="ComparisonError"
            )
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            pytest.raises(RuntimeError, match="sync failed: Failed - ComparisonError"),
        ):
            await argo_manager.wait_for_application_synced("my-app", timeout=10, poll_interval=1)

    @pytest.mark.asyncio
    async def test_sync_error_raises_runtime_error(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should raise RuntimeError when operationState.phase is Error."""
        mock_connector.get_application_status = AsyncMock(
            return_value=_make_status("OutOfSync", "Missing", operation_phase="Error")
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            pytest.raises(RuntimeError, match="sync failed: Error"),
        ):
            await argo_manager.wait_for_application_synced("my-app", timeout=10, poll_interval=1)

    @pytest.mark.asyncio
    async def test_health_degraded_raises_runtime_error(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should raise RuntimeError when health status is Degraded."""
        mock_connector.get_application_status = AsyncMock(return_value=_make_status("Synced", "Degraded"))

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            pytest.raises(RuntimeError, match="is degraded"),
        ):
            await argo_manager.wait_for_application_synced("my-app", timeout=10, poll_interval=1)

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should raise TimeoutError when app never becomes synced within timeout."""
        mock_connector.get_application_status = AsyncMock(return_value=_make_status("OutOfSync", "Progressing"))

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(TimeoutError, match="Timeout waiting for application"),
        ):
            await argo_manager.wait_for_application_synced("my-app", timeout=5, poll_interval=2)

    @pytest.mark.asyncio
    async def test_permission_error_retried_then_succeeds(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should retry on PermissionError and return True once synced."""
        mock_connector.get_application_status = AsyncMock(
            side_effect=[
                PermissionError("forbidden"),
                PermissionError("forbidden"),
                _make_status("Synced", "Healthy"),
            ]
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await argo_manager.wait_for_application_synced("my-app", timeout=30, poll_interval=2)

        assert result is True
        assert mock_connector.get_application_status.call_count == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_login_failure_raises_runtime_error(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should raise RuntimeError when ArgoCD login fails."""
        mock_connector.login = AsyncMock(return_value=False)

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            pytest.raises(RuntimeError, match="Failed to login to ArgoCD"),
        ):
            await argo_manager.wait_for_application_synced("my-app", timeout=10, poll_interval=1)

        mock_connector.get_application_status.assert_not_called()
