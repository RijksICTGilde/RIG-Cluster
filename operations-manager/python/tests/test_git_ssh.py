"""
Test the GitConnector with SSH authentication.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.git import SSH_KNOWN_HOSTS_PATH, GitConnector


@pytest.mark.asyncio
async def test_git_connector_ssh_command():
    """De SSH-opdracht draagt de sleutel, de poort en de hostsleutelcontrole."""
    ssh_key_path = "/path/to/ssh/key"
    connector = GitConnector(repo_url="ssh://git@example.com:2222/repo.git", ssh_key_path=ssh_key_path)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_process

        await connector._run_git_command(["status"])

        env = mock_exec.call_args[1]["env"]

    assert ssh_key_path in env["GIT_SSH_COMMAND"]
    assert "-p 2222" in env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=accept-new" in env["GIT_SSH_COMMAND"]
    assert f"UserKnownHostsFile={SSH_KNOWN_HOSTS_PATH}" in env["GIT_SSH_COMMAND"]


@pytest.mark.asyncio
async def test_ssh_connector_sends_no_http_credentials():
    """Een SSH-repo krijgt geen Basic-auth-header mee."""
    connector = GitConnector(repo_url="ssh://git@example.com/repo.git", ssh_key_path="/path/to/ssh/key")

    assert connector._credential_env() == {}
    assert connector.ssh_port == 22
