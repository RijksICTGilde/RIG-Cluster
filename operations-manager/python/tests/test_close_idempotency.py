"""close() is safe to call twice on the connectors that clean up a temp workspace."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGitConnectorCloseIdempotency:
    """Verify GitConnector.close() is safe to call multiple times."""

    @pytest.mark.asyncio
    async def test_close_twice_only_cleans_once(self):
        with (
            patch("opi.connectors.git.GitConnector._parse_git_url") as mock_parse,
            patch("tempfile.mkdtemp", return_value="/tmp/fake-dir"),
            patch("os.path.exists", return_value=True),
            patch("shutil.rmtree") as mock_rmtree,
            patch("opi.core.config.settings") as mock_settings,
        ):
            mock_settings.TEMP_DIR = "/tmp"
            mock_parse.return_value = MagicMock()

            from opi.connectors.git import GitConnector

            connector = GitConnector.__new__(GitConnector)
            connector.repo_url = "https://example.com/repo.git"
            connector.name = "test-repo"
            connector.project_name = "test"
            connector._GitConnector__working_dir = "/tmp/fake-dir"
            connector._repo_cloned = False
            connector._fetched_in_session = False
            connector._closed = False
            connector._git_user_configured = False
            connector.should_cleanup = True

            with patch.object(connector, "has_changes", new_callable=AsyncMock, return_value=False):
                await connector.close()
                assert connector._closed is True
                mock_rmtree.assert_called_once()

                mock_rmtree.reset_mock()
                await connector.close()
                # Second call should not attempt cleanup
                mock_rmtree.assert_not_called()


class TestProjectManagerCloseIdempotency:
    """Verify ProjectManager.close() is safe to call multiple times."""

    @pytest.mark.asyncio
    async def test_close_twice_only_cleans_once(self):
        with patch("opi.manager.project_manager.ProjectManager.__init__", return_value=None):
            from opi.manager.project_manager import ProjectManager

            pm = ProjectManager.__new__(ProjectManager)
            pm._closed = False
            pm.close_git_connector_for_project_files = AsyncMock()
            pm.close_argo_workspace = AsyncMock()
            pm.close_deployment_workspaces = AsyncMock()
            pm._database_manager = None

            await pm.close()
            assert pm._closed is True
            pm.close_git_connector_for_project_files.assert_awaited_once()
            pm.close_argo_workspace.assert_awaited_once()
            pm.close_deployment_workspaces.assert_awaited_once()

            # Reset mocks and call again
            pm.close_git_connector_for_project_files.reset_mock()
            pm.close_argo_workspace.reset_mock()
            pm.close_deployment_workspaces.reset_mock()

            await pm.close()
            # Second call should not invoke any cleanup
            pm.close_git_connector_for_project_files.assert_not_awaited()
            pm.close_argo_workspace.assert_not_awaited()
            pm.close_deployment_workspaces.assert_not_awaited()
