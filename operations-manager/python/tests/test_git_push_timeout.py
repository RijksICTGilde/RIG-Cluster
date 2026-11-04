"""Tests for the network timeout on GitConnector's push.

The 4 November incident: a push to an unreachable remote only failed on curl's own
connect timeout of 300 seconds, and held the push lock for that ref all that time.
GIT_PUSH_TIMEOUT_SECONDS bounds the push itself and the fetch of the rebase that the
retry loop does; the projects-repo push of ProjectStore runs through this same path.
"""

import asyncio
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import (
    GIT_PUSH_TIMEOUT_SECONDS,
    GitCommandTimeoutError,
    GitConnector,
    _push_locks,
    push_lock_for,
)

#: Without the timeout these tests do not fail but hang on their own fake process, so
#: they carry a pytest timeout: a regression is then red in seconds, not after an hour.
TEST_TIMEOUT_SECONDS = 15

if TYPE_CHECKING:
    from pathlib import Path

REPO_URL = "https://forge.example/rig/zad-projects.git"


@pytest.fixture(autouse=True)
def _clear_push_locks() -> None:
    _push_locks.clear()
    yield
    _push_locks.clear()


@pytest.fixture
def _short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import opi.connectors.git as git_module

    monkeypatch.setattr(git_module, "GIT_PUSH_TIMEOUT_SECONDS", 0.05)


class _HangingProcess:
    """A git process that never answers, and remembers that it was killed."""

    def __init__(self) -> None:
        self.killed = False

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return -9


class _InstantProcess:
    """A git process that succeeds without output."""

    returncode = 0

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        return b"", b""


def _connector(tmp_path: Path) -> GitConnector:
    connector = GitConnector(repo_url=REPO_URL, branch="main", working_dir=str(tmp_path), project_name="etevao-okj")

    async def already_cloned() -> None:
        return None

    async def has_commits(_branch: str) -> bool:
        return True

    connector.ensure_repo_cloned = already_cloned  # type: ignore[method-assign]
    connector._has_commits_to_push = has_commits  # type: ignore[method-assign]
    return connector


def _hang_on(subcommand: str, monkeypatch: pytest.MonkeyPatch) -> tuple[list[_HangingProcess], list[list[str]]]:
    """Let ``git <subcommand>`` hang; every other git command succeeds at once."""
    import opi.connectors.git as git_module

    hanging: list[_HangingProcess] = []
    commands: list[list[str]] = []

    async def fake_exec(*cmd: str, **_kwargs: object):
        commands.append(list(cmd))
        if subcommand in cmd:
            process = _HangingProcess()
            hanging.append(process)
            return process
        return _InstantProcess()

    monkeypatch.setattr(git_module.asyncio, "create_subprocess_exec", fake_exec)
    return hanging, commands


@pytest.mark.asyncio
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
@pytest.mark.usefixtures("_short_timeout")
async def test_a_push_without_an_answer_fails_fast_and_kills_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = _connector(tmp_path)
    hanging, commands = _hang_on("push", monkeypatch)

    with pytest.raises(GitCommandTimeoutError) as caught:
        await connector.push_changes()

    message = str(caught.value)
    assert "push origin main" in message
    assert "forge.example" in message, "the message must name the server that did not answer"
    assert "0.05s" in message
    assert "got no response" in message, "a killed push may still have landed server-side"
    assert hanging[0].killed, "the hanging git process must be killed"
    assert len([cmd for cmd in commands if "push" in cmd]) == 1, "a timeout is not retried"
    assert not any("rebase" in cmd for cmd in commands), "a timeout must not be read as a non-fast-forward"
    assert not push_lock_for(REPO_URL, "main").locked(), "the push lock must be free afterwards"


@pytest.mark.asyncio
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
@pytest.mark.usefixtures("_short_timeout")
async def test_the_next_push_gets_through_after_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector(tmp_path)
    _hang_on("push", monkeypatch)
    with pytest.raises(GitCommandTimeoutError):
        await connector.push_changes()

    _hang_on("nothing-hangs-here", monkeypatch)
    await _connector(tmp_path).push_changes()


@pytest.mark.asyncio
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
@pytest.mark.usefixtures("_short_timeout")
async def test_the_fetch_of_the_rebase_is_bounded_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A push rejected as non-fast-forward rebases, and that fetch is network too."""
    import opi.connectors.git as git_module

    connector = _connector(tmp_path)
    hanging: list[_HangingProcess] = []

    class _RejectingProcess:
        returncode = 1

        async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
            return b"", b"! [rejected] main -> main (non-fast-forward)"

    async def fake_exec(*cmd: str, **_kwargs: object):
        if "push" in cmd:
            return _RejectingProcess()
        if "fetch" in cmd:
            process = _HangingProcess()
            hanging.append(process)
            return process
        return _InstantProcess()

    monkeypatch.setattr(git_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(GitCommandTimeoutError) as caught:
        await connector.push_changes()

    assert "fetch origin main" in str(caught.value)
    assert hanging[0].killed


@pytest.mark.asyncio
async def test_a_command_without_a_timeout_still_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other caller of _run_git_command keeps its unbounded wait."""
    import opi.connectors.git as git_module

    seen: list[float | None] = []
    real = git_module.run_git

    async def recording_run_git(args, *, cwd, env=None, stdin=None, timeout=None):
        seen.append(timeout)
        return await real(args, cwd=cwd, env=env, stdin=stdin, timeout=timeout)

    monkeypatch.setattr(git_module, "run_git", recording_run_git)
    _hang_on("nothing-hangs-here", monkeypatch)

    connector = _connector(tmp_path)
    stdout, stderr, code = await connector._run_git_command(["status", "--porcelain"])

    assert (stdout, stderr, code) == ("", "", 0)
    assert seen == [None]


def test_the_default_timeout_is_a_minute() -> None:
    assert GIT_PUSH_TIMEOUT_SECONDS == 60
