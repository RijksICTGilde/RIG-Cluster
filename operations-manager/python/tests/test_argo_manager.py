"""Tests for ArgoManager.wait_for_application_synced and ArgoConnector.refresh_application."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.argo import ArgoConnector
from opi.manager.argo_manager import ArgoManager


def _make_status(
    sync: str,
    health: str,
    operation_phase: str | None = None,
    operation_message: str = "",
    reconciled_at: str | None = None,
) -> dict:
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
    if reconciled_at:
        status["status"]["reconciledAt"] = reconciled_at
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

    @pytest.mark.asyncio
    async def test_on_progressing_called_when_progressing_and_fresh(
        self, argo_manager: ArgoManager, mock_connector: AsyncMock
    ):
        """Should call on_progressing callback when health is Progressing and status is fresh."""
        mock_connector.get_application_status = AsyncMock(
            side_effect=[
                _make_status("Synced", "Progressing", reconciled_at="2026-03-17T10:01:00Z"),
                _make_status("Synced", "Healthy", reconciled_at="2026-03-17T10:01:00Z"),
            ]
        )
        callback = AsyncMock()

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await argo_manager.wait_for_application_synced(
                "my-app",
                timeout=30,
                poll_interval=2,
                refreshed_after="2026-03-17T10:00:00Z",
                on_progressing=callback,
            )

        assert result is True
        callback.assert_called_once_with(2)  # elapsed_time after first poll_interval

    @pytest.mark.asyncio
    async def test_on_progressing_called_even_when_stale(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should call on_progressing even when status is stale.

        The callback uses kubectl to check pod health directly, so it
        does not depend on ArgoCD's reconciliation freshness.
        """
        mock_connector.get_application_status = AsyncMock(
            side_effect=[
                # Stale Progressing — callback should still fire
                _make_status("Synced", "Progressing", reconciled_at="2026-03-17T10:00:00Z"),
                # Fresh and healthy
                _make_status("Synced", "Healthy", reconciled_at="2026-03-17T10:01:00Z"),
            ]
        )
        callback = AsyncMock()

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await argo_manager.wait_for_application_synced(
                "my-app",
                timeout=30,
                poll_interval=2,
                refreshed_after="2026-03-17T10:00:00Z",
                on_progressing=callback,
            )

        assert result is True
        callback.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_on_progressing_exception_propagates(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Exceptions from on_progressing should propagate to caller."""
        mock_connector.get_application_status = AsyncMock(
            return_value=_make_status("Synced", "Progressing", reconciled_at="2026-03-17T10:01:00Z"),
        )

        class CustomError(Exception):
            pass

        callback = AsyncMock(side_effect=CustomError("OOM detected"))

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(CustomError, match="OOM detected"),
        ):
            await argo_manager.wait_for_application_synced(
                "my-app",
                timeout=30,
                poll_interval=2,
                refreshed_after="2026-03-17T10:00:00Z",
                on_progressing=callback,
            )


class TestStaleStatusProtection:
    """Tests for the refreshed_after parameter that prevents acting on stale status."""

    @pytest.mark.asyncio
    async def test_degraded_ignored_while_status_is_stale(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should not raise on Degraded when reconciledAt hasn't moved past refreshed_after."""
        mock_connector.get_application_status = AsyncMock(
            side_effect=[
                # First poll: stale Degraded (reconciledAt == refreshed_after)
                _make_status("Synced", "Degraded", reconciled_at="2026-03-17T10:00:00Z"),
                # Second poll: fresh and healthy
                _make_status("Synced", "Healthy", reconciled_at="2026-03-17T10:01:00Z"),
            ]
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await argo_manager.wait_for_application_synced(
                "my-app", timeout=30, poll_interval=2, refreshed_after="2026-03-17T10:00:00Z"
            )

        assert result is True
        assert mock_connector.get_application_status.call_count == 2

    @pytest.mark.asyncio
    async def test_degraded_raised_once_status_is_fresh(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should raise on Degraded once reconciledAt has moved past refreshed_after."""
        mock_connector.get_application_status = AsyncMock(
            return_value=_make_status("Synced", "Degraded", reconciled_at="2026-03-17T10:02:00Z"),
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            pytest.raises(RuntimeError, match="is degraded"),
        ):
            await argo_manager.wait_for_application_synced(
                "my-app", timeout=10, poll_interval=1, refreshed_after="2026-03-17T10:00:00Z"
            )

    @pytest.mark.asyncio
    async def test_sync_failed_ignored_while_stale(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should not raise on Failed operationState when status is stale."""
        mock_connector.get_application_status = AsyncMock(
            side_effect=[
                # Stale Failed status
                _make_status(
                    "OutOfSync",
                    "Progressing",
                    operation_phase="Failed",
                    operation_message="old error",
                    reconciled_at="2026-03-17T10:00:00Z",
                ),
                # Fresh and healthy
                _make_status("Synced", "Healthy", reconciled_at="2026-03-17T10:01:00Z"),
            ]
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await argo_manager.wait_for_application_synced(
                "my-app", timeout=30, poll_interval=2, refreshed_after="2026-03-17T10:00:00Z"
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_no_refreshed_after_preserves_existing_behavior(
        self, argo_manager: ArgoManager, mock_connector: AsyncMock
    ):
        """Without refreshed_after, Degraded should raise immediately (backwards compat)."""
        mock_connector.get_application_status = AsyncMock(
            return_value=_make_status("Synced", "Degraded"),
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            pytest.raises(RuntimeError, match="is degraded"),
        ):
            await argo_manager.wait_for_application_synced("my-app", timeout=10, poll_interval=1)

    @pytest.mark.asyncio
    async def test_stale_then_fresh_degraded(self, argo_manager: ArgoManager, mock_connector: AsyncMock):
        """Should skip stale Degraded, then raise once fresh Degraded arrives."""
        mock_connector.get_application_status = AsyncMock(
            side_effect=[
                # Stale Degraded
                _make_status("Synced", "Degraded", reconciled_at="2026-03-17T10:00:00Z"),
                # Fresh but still Degraded
                _make_status("Synced", "Degraded", reconciled_at="2026-03-17T10:01:00Z"),
            ]
        )

        with (
            patch("opi.connectors.argo.create_argo_connector", return_value=mock_connector),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(RuntimeError, match="is degraded"),
        ):
            await argo_manager.wait_for_application_synced(
                "my-app", timeout=30, poll_interval=2, refreshed_after="2026-03-17T10:00:00Z"
            )


class TestRefreshApplicationReturnsReconciledAt:
    """Tests for ArgoConnector.refresh_application returning reconciledAt."""

    @pytest.fixture
    def connector(self) -> ArgoConnector:
        return ArgoConnector(server_host="localhost", server_port=8080, username="admin", password="admin")

    @pytest.mark.asyncio
    async def test_returns_reconciled_at_on_success(self, connector: ArgoConnector):
        """Should parse and return reconciledAt from the refresh response."""
        response_body = json.dumps(
            {
                "status": {
                    "reconciledAt": "2026-03-17T10:54:33Z",
                    "sync": {"status": "Synced"},
                    "health": {"status": "Healthy"},
                }
            }
        )

        with patch.object(
            connector, "_make_authenticated_request", new_callable=AsyncMock, return_value=(200, response_body)
        ):
            result = await connector.refresh_application("my-app")

        assert result == "2026-03-17T10:54:33Z"

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, connector: ArgoConnector):
        """Should return None when the refresh API call fails."""
        with patch.object(
            connector, "_make_authenticated_request", new_callable=AsyncMock, return_value=(500, "error")
        ):
            result = await connector.refresh_application("my-app")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, connector: ArgoConnector):
        """Should return None when the request raises an exception."""
        with patch.object(
            connector, "_make_authenticated_request", new_callable=AsyncMock, side_effect=Exception("connection error")
        ):
            result = await connector.refresh_application("my-app")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_reconciled_at_missing(self, connector: ArgoConnector):
        """Should return None when the response has no reconciledAt field."""
        response_body = json.dumps({"status": {"sync": {"status": "Synced"}}})

        with patch.object(
            connector, "_make_authenticated_request", new_callable=AsyncMock, return_value=(200, response_body)
        ):
            result = await connector.refresh_application("my-app")

        assert result is None

    @pytest.mark.asyncio
    async def test_truthiness_for_existing_callers(self, connector: ArgoConnector):
        """Callers that do `if not await refresh_application(...)` should still work."""
        response_body = json.dumps({"status": {"reconciledAt": "2026-03-17T10:00:00Z"}})

        with patch.object(
            connector, "_make_authenticated_request", new_callable=AsyncMock, return_value=(200, response_body)
        ):
            result = await connector.refresh_application("my-app")

        # String is truthy, so `if not result` is False — same as old `True` return
        assert result
        assert result
