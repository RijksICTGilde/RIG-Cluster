"""Tests for opi.connectors.git module.

Focuses on: command construction, repository creation, SSH setup.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.git import GitConnector


class TestCreateRepository:
    """create_repository builds SSH commands to initialize a bare Git repo on a remote server."""

    @pytest.mark.asyncio
    async def test_git_init_command_is_valid(self):
        """The git init command must be 'git init', not 'git-init' which doesn't exist."""
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            # First call: check if repo exists (return non-zero = doesn't exist)
            check_process = AsyncMock()
            check_process.returncode = 1
            check_process.communicate.return_value = (b"", b"")

            # Subsequent calls: mkdir and git init (succeed)
            cmd_process = AsyncMock()
            cmd_process.returncode = 0
            cmd_process.communicate.return_value = (b"", b"")

            mock_exec.side_effect = [check_process, cmd_process, cmd_process]

            result = await GitConnector.create_repository(
                server_host="git.example.com",
                repo_name="test-repo",
                ssh_key_path="/path/to/key",
            )

            assert result is True

            # Inspect the git init call (3rd call - after check and mkdir)
            calls = mock_exec.call_args_list
            assert len(calls) == 3, f"Expected 3 subprocess calls, got {len(calls)}"

            # The third call should be the git init command
            git_init_call_args = calls[2][0]
            # Find the argument that contains 'git' and 'init'
            remote_cmd = git_init_call_args[-1]  # last arg is the remote command string
            assert "git init" in remote_cmd, f"Expected 'git init' in remote command, got: {remote_cmd}"
            assert "git-init" not in remote_cmd, f"Found invalid 'git-init' (should be 'git init') in: {remote_cmd}"
