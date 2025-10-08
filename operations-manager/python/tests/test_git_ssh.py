"""
Test the GitConnector with SSH authentication.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.git import GitConnector, create_git_connector, create_git_repository


@pytest.mark.asyncio
async def test_git_connector_ssh_command():
    """Test that GitConnector properly sets up SSH command with key path."""
    # Create a test SSH key path
    ssh_key_path = "/path/to/ssh/key"
    ssh_port = 2222

    # Create a GitConnector with SSH key
    connector = GitConnector(repo_url="ssh://git@example.com/repo.git", ssh_key_path=ssh_key_path, ssh_port=ssh_port)

    # Mock the asyncio create_subprocess_exec to capture environment variables
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        # Setup mock to return a process with the expected communicate method
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_process

        # Call _run_git_command which should set up the SSH command
        await connector._run_git_command(["status"])

        # Check if the mock was called with the expected environment variable
        call_kwargs = mock_exec.call_args[1]
        env = call_kwargs.get("env", {})

        # Verify SSH command was properly set in environment
        assert "GIT_SSH_COMMAND" in env
        assert ssh_key_path in env["GIT_SSH_COMMAND"]
        assert f"-p {ssh_port}" in env["GIT_SSH_COMMAND"]
        assert "StrictHostKeyChecking=no" in env["GIT_SSH_COMMAND"]


@pytest.mark.asyncio
async def test_git_connector_create_repository():
    """Test create_repository with SSH key."""
    # Define test parameters
    server_host = "example.com"
    repo_name = "test-repo"
    ssh_key_path = "/path/to/ssh/key"
    ssh_port = 2222

    # Mock asyncio.create_subprocess_exec
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        # Setup mock process
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_process

        # Call create_repository
        result = await create_git_repository(
            server_host=server_host, repo_name=repo_name, ssh_key_path=ssh_key_path, ssh_port=ssh_port
        )

        # Verify result
        assert result is True

        # Check that correct commands were called
        assert mock_exec.call_count == 2  # Should call twice for the two commands

        # Verify SSH key and port were used
        for call in mock_exec.call_args_list:
            args = call[0]
            # Check if SSH command includes key path and port
            assert "-i" in args
            assert ssh_key_path in args
            assert "-p" in args
            assert str(ssh_port) in args


@pytest.mark.asyncio
async def test_create_git_connector_with_ssh():
    """Test that create_git_connector properly passes SSH parameters."""
    # Test parameters
    repo_url = "ssh://git@example.com/repo.git"
    ssh_key_path = "/path/to/ssh/key"
    ssh_port = 2222

    # Call create_git_connector
    connector = create_git_connector(repo_url=repo_url, ssh_key_path=ssh_key_path, ssh_port=ssh_port)

    # Verify SSH parameters were passed
    assert connector.ssh_key_path == ssh_key_path
    assert connector.ssh_port == ssh_port
