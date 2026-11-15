"""A test Postgres per session, not a shared one on a fixed port (RC-478).

Two dclaude sessions share the docker daemon and the host network. Without a name of its
own and a free port, one pulls the other's container out from under it, or collides on the
same port. `tests/conftest.py` solves that with the session name in the container name
and a port Docker picks itself; `Taskfile.yaml` reads the same name for
`task test-db-stop`/`test-db-reset`.

The name derivation lives at module level in `conftest.py` and reads the environment at
import time, so the only way to measure it under a different environment is a fresh
process (the same approach `test_een_naam_van_een_gestorven_run_wordt_als_wees_herkend`
in `test_orm_database_opruiming.py` already takes). The port and mount behavior of
`_zorg_voor_container` can be measured in-process, by replacing `_docker`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import yaml
from tests.image_pins import REPO_ROOT

sys.path.insert(0, os.path.dirname(__file__))
import conftest

if TYPE_CHECKING:
    import pytest

TASKFILE = REPO_ROOT / "Taskfile.yaml"

_NAME_SCRIPT = """
import sys
sys.path.insert(0, {testdir!r})
import conftest
print(conftest.ZAD_TEST_PG_CONTAINER)
"""


def _container_name(env_overrides: dict[str, str]) -> str:
    testdir = os.path.dirname(__file__)
    full_env = os.environ.copy()
    full_env.pop("HOSTNAME", None)
    full_env.pop("ZAD_TEST_PG_CONTAINER", None)
    full_env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", _NAME_SCRIPT.format(testdir=testdir)],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


class TestContainerNameFollowsTheSession:
    def test_without_hostname_the_name_stays_fixed(self) -> None:
        assert _container_name({}) == "zad-test-postgres"

    def test_a_hostname_outside_a_dclaude_session_changes_nothing(self) -> None:
        assert _container_name({"HOSTNAME": "roberts-laptop"}) == "zad-test-postgres"

    def test_a_dclaude_session_gets_its_own_container(self) -> None:
        name = _container_name({"HOSTNAME": "dclaude-RIG-Cluster-rc478-test"})
        assert name == "zad-test-postgres-dclaude-RIG-Cluster-rc478-test"

    def test_an_explicit_name_wins_over_the_derivation(self) -> None:
        name = _container_name({"HOSTNAME": "dclaude-abc", "ZAD_TEST_PG_CONTAINER": "my-own-name"})
        assert name == "my-own-name"


def _docker_run_command(monkeypatch: pytest.MonkeyPatch, pinned_port: str | None) -> tuple[str, ...]:
    """Runs `_zorg_voor_container` against a fake docker and returns its `run` command."""
    monkeypatch.setattr(conftest, "ZAD_TEST_PG_PORT", pinned_port)
    monkeypatch.setattr(conftest, "_container_staat", lambda: (False, ""))
    monkeypatch.setattr(conftest, "_wacht_tot_hij_luistert", lambda: None)
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "run":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="0.0.0.0:49999\n", stderr="")

    monkeypatch.setattr(conftest, "_docker", fake_docker)

    conftest._zorg_voor_container()

    return next(a for a in calls if a[0] == "run")


class TestTheContainerPublishesWithoutAMount:
    def test_without_a_pinned_port_docker_picks_a_free_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_command = _docker_run_command(monkeypatch, None)
        assert "127.0.0.1::5432" in run_command

    def test_with_a_pinned_port_it_publishes_exactly_that_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_command = _docker_run_command(monkeypatch, "12345")
        assert "127.0.0.1:12345:5432" in run_command

    def test_it_never_publishes_on_every_interface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for pinned_port in (None, "12345"):
            run_command = _docker_run_command(monkeypatch, pinned_port)
            port_argument = run_command[run_command.index("-p") + 1]
            assert port_argument.startswith("127.0.0.1"), port_argument

    def test_it_no_longer_gets_a_mount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_command = _docker_run_command(monkeypatch, None)
        assert "-v" not in run_command

    def test_the_startup_script_travels_as_an_argument_not_a_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_command = _docker_run_command(monkeypatch, None)
        assert run_command[-3:] == ("bash", "-c", conftest.ZAD_TEST_PG_START.read_text())


def _taskfile_vars() -> dict[str, Any]:
    return yaml.safe_load(TASKFILE.read_text())["vars"]


def _taskfile_tasks() -> dict[str, Any]:
    return yaml.safe_load(TASKFILE.read_text())["tasks"]


def _test_pg_container_name(hostname: str) -> str:
    sh = _taskfile_vars()["TEST_PG_CONTAINER"]["sh"]
    result = subprocess.run(
        ["bash", "-c", sh],
        capture_output=True,
        text=True,
        env={**os.environ, "HOSTNAME": hostname},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestTheTaskfileFollowsTheSameName:
    def test_outside_a_session_it_is_the_shared_name(self) -> None:
        assert _test_pg_container_name("roberts-laptop") == "zad-test-postgres"

    def test_inside_a_dclaude_session_it_follows_the_session(self) -> None:
        assert (
            _test_pg_container_name("dclaude-RIG-Cluster-rc478-test")
            == "zad-test-postgres-dclaude-RIG-Cluster-rc478-test"
        )

    def test_test_db_stop_removes_this_sessions_name(self) -> None:
        cmds = _taskfile_tasks()["test-db-stop"]["cmds"]
        assert any("{{.TEST_PG_CONTAINER}}" in cmd for cmd in cmds), cmds

    def test_test_db_reset_removes_the_same_name_and_selects_the_wide_layer(self) -> None:
        cmds = [cmd for cmd in _taskfile_tasks()["test-db-reset"]["cmds"] if isinstance(cmd, str)]
        assert any("{{.TEST_PG_CONTAINER}}" in cmd for cmd in cmds), cmds
        assert any("-m wide" in cmd for cmd in cmds), (
            f"test-db-reset runs without -m wide, and the default addopts deselects the ORM tests: {cmds}"
        )
