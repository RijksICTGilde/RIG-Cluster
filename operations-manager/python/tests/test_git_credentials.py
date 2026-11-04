"""Credentials gaan via de omgeving naar één host, nooit via de URL of het argument."""

import base64
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.git import GitConnector, _obfuscate_git_command, strip_url_credentials

WACHTWOORD = "zeer-geheim-token"


def _connector(repo_url: str = "https://forgejo.example.com/zad/projects.git", **kwargs) -> GitConnector:
    return GitConnector(repo_url=repo_url, username="zad", password=f"plain:{WACHTWOORD}", **kwargs)


async def _run_and_capture(connector: GitConnector, args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Draai een git-commando met een gemockt subprocess en geef argumenten en omgeving terug."""
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        mock_exec.return_value = process

        await connector._run_git_command(args)

        return list(mock_exec.call_args[0]), mock_exec.call_args[1]["env"]


@pytest.mark.asyncio
async def test_wachtwoord_staat_in_geen_enkel_argument():
    connector = _connector()

    argumenten, _ = await _run_and_capture(connector, ["ls-remote", connector.repo_url_with_path])

    assert WACHTWOORD not in " ".join(argumenten)
    assert connector.repo_url_with_path == "https://forgejo.example.com/zad/projects.git"


@pytest.mark.asyncio
async def test_omgeving_bindt_de_header_aan_de_basis_url():
    connector = _connector()

    _, env = await _run_and_capture(connector, ["ls-remote", connector.repo_url_with_path])

    token = base64.b64encode(f"zad:{WACHTWOORD}".encode()).decode()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://forgejo.example.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {token}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.asyncio
async def test_de_poort_hoort_bij_de_basis_url():
    connector = _connector("http://forgejo.example.com:3000/zad/projects.git")

    _, env = await _run_and_capture(connector, ["ls-remote", connector.repo_url_with_path])

    assert env["GIT_CONFIG_KEY_0"] == "http.http://forgejo.example.com:3000/.extraHeader"


@pytest.mark.asyncio
async def test_zonder_wachtwoord_geen_header():
    connector = GitConnector(repo_url="https://forgejo.example.com/zad/projects.git")

    _, env = await _run_and_capture(connector, ["status"])

    assert "GIT_CONFIG_KEY_0" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.asyncio
async def test_credentials_uit_de_url_gaan_ook_naar_de_omgeving():
    """Een repo-URL met userinfo levert dezelfde header op, en de URL zelf wordt schoon."""
    connector = GitConnector(repo_url=f"https://zad:{WACHTWOORD}@forgejo.example.com/zad/projects.git")

    argumenten, env = await _run_and_capture(connector, ["ls-remote", connector.repo_url_with_path])

    token = base64.b64encode(f"zad:{WACHTWOORD}".encode()).decode()
    assert WACHTWOORD not in " ".join(argumenten)
    assert connector.repo_url_with_path == "https://forgejo.example.com/zad/projects.git"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {token}"


@pytest.mark.asyncio
async def test_een_ipv6_literal_houdt_zijn_blokhaken():
    """parsed.hostname levert ::1:3000 op; de sleutel en de URL moeten bruikbaar blijven."""
    connector = _connector("https://[::1]:3000/zad/projects.git")

    _, env = await _run_and_capture(connector, ["ls-remote", connector.repo_url_with_path])

    assert env["GIT_CONFIG_KEY_0"] == "http.https://[::1]:3000/.extraHeader"
    assert connector.repo_url_with_path == "https://[::1]:3000/zad/projects.git"


def test_strip_url_credentials_laat_ssh_met_rust():
    assert strip_url_credentials("ssh://git@example.com/repo.git") == "ssh://git@example.com/repo.git"
    assert strip_url_credentials("git@example.com:zad/repo.git") == "git@example.com:zad/repo.git"


@pytest.mark.parametrize(
    ("regel", "verwacht"),
    [
        ("git clone https://zad:geheim@host/r.git", "git clone https://zad:***@host/r.git"),
        ("git clone http://zad:geheim@host/r.git", "git clone http://zad:***@host/r.git"),
        ("git clone https://ghp_abc123@host/r.git", "git clone https://***@host/r.git"),
        ("git clone http://ghp_abc123@host/r.git", "git clone http://***@host/r.git"),
        ("git clone https://host/r.git", "git clone https://host/r.git"),
    ],
)
def test_obfuscate_git_command(regel: str, verwacht: str):
    assert _obfuscate_git_command(regel) == verwacht
