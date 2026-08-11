"""Tests for the build guards that keep a sandbox build from taking the machine down.

Two things are guarded here:
  1. scripts/build-preflight.sh - refuses to start when too little memory is free.
  2. The Taskfile sandbox build tasks - they must use the memory-bounded builder,
     the cache and the preflight check. Losing any of those silently brings back the
     unbounded, cacheless build that trampled the shared dev server.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = REPO_ROOT / "scripts" / "build-preflight.sh"
TASKFILE = REPO_ROOT / "Taskfile.yaml"

BUILD_TASK = "sandbox:build-operations-manager-image"
UPDATE_TASK = "sandbox:update-operations-manager"
BUILDER_TASK = "sandbox:build-builder"


def _meminfo(available_kb: int) -> str:
    return f"MemTotal:       16257452 kB\nMemFree:         1000000 kB\nMemAvailable:   {available_kb} kB\n"


def _run_preflight(tmp_path: Path, available_kb: int | None, **env_extra: str) -> subprocess.CompletedProcess[str]:
    meminfo = tmp_path / "meminfo"
    if available_kb is not None:
        meminfo.write_text(_meminfo(available_kb))
    loadavg = tmp_path / "loadavg"
    loadavg.write_text("34.80 30.10 22.00 8/1234 5678\n")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "BUILD_PREFLIGHT_MEMINFO": str(meminfo),
        "BUILD_PREFLIGHT_LOADAVG": str(loadavg),
        "BUILD_MIN_AVAILABLE_MB": "6144",
        **env_extra,
    }
    if env.get("BUILD_MIN_AVAILABLE_MB") == "":
        del env["BUILD_MIN_AVAILABLE_MB"]
    return subprocess.run(
        ["bash", str(PREFLIGHT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@pytest.fixture(scope="module")
def taskfile() -> dict:
    return yaml.safe_load(TASKFILE.read_text())


@pytest.fixture(scope="module")
def bash_available() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")


@pytest.mark.usefixtures("bash_available")
class TestBuildPreflight:
    def test_allows_build_when_enough_memory_is_free(self, tmp_path: Path) -> None:
        # 10 GB available, minimum is 6 GB
        result = _run_preflight(tmp_path, available_kb=10 * 1024 * 1024)

        assert result.returncode == 0
        assert "10240 MB" in result.stdout

    def test_refuses_build_when_too_little_memory_is_free(self, tmp_path: Path) -> None:
        # 1 GB available - the state the shared server was in when it nearly went down
        result = _run_preflight(tmp_path, available_kb=1024 * 1024)

        assert result.returncode == 1
        assert "TE WEINIG VRIJ GEHEUGEN" in result.stderr

    def test_refusal_reports_what_is_running(self, tmp_path: Path) -> None:
        """A bare number gives no address - the refusal must say who is in the way."""
        result = _run_preflight(tmp_path, available_kb=1024 * 1024)

        assert "Nu draait er:" in result.stderr

    def test_refusal_shows_the_load_average(self, tmp_path: Path) -> None:
        result = _run_preflight(tmp_path, available_kb=1024 * 1024)

        assert "34.80" in result.stdout + result.stderr

    def test_skip_flag_overrides_the_refusal(self, tmp_path: Path) -> None:
        result = _run_preflight(tmp_path, available_kb=1024 * 1024, BUILD_PREFLIGHT_SKIP="1")

        assert result.returncode == 0

    def test_threshold_is_configurable(self, tmp_path: Path) -> None:
        result = _run_preflight(tmp_path, available_kb=2 * 1024 * 1024, BUILD_MIN_AVAILABLE_MB="1024")

        assert result.returncode == 0

    def test_unreadable_meminfo_does_not_block_the_build(self, tmp_path: Path) -> None:
        """On a machine without /proc/meminfo the guard steps aside instead of failing."""
        result = _run_preflight(tmp_path, available_kb=None)

        assert result.returncode == 0

    def test_default_threshold_is_reachable_on_the_shared_server(self, tmp_path: Path) -> None:
        """The default must clear on a machine that never has much MemAvailable.

        The shared dev server sits between 3 and 5.5 GB available all day, so a default of
        6144 MB refused every build - while a cold build was measured to peak at 427 MB and
        to pull MemAvailable down by less than 500 MB. A threshold nobody can ever meet is
        not a guard, it is an outage: it pushes people to BUILD_PREFLIGHT_SKIP=1 by habit.
        """
        result = _run_preflight(tmp_path, available_kb=3 * 1024 * 1024, BUILD_MIN_AVAILABLE_MB="")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_default_threshold_still_refuses_a_genuinely_empty_machine(self, tmp_path: Path) -> None:
        """Lowering the default must not turn the guard off altogether."""
        result = _run_preflight(tmp_path, available_kb=512 * 1024, BUILD_MIN_AVAILABLE_MB="")

        assert result.returncode == 1
        assert "TE WEINIG VRIJ GEHEUGEN" in result.stderr

    def test_script_is_executable(self) -> None:
        assert PREFLIGHT.stat().st_mode & 0o111


class TestTaskfileBuildTasks:
    def _cmds(self, taskfile: dict, name: str) -> str:
        # width: geen regelafbreking, anders knipt safe_dump midden door een vlag heen
        return yaml.safe_dump(taskfile["tasks"][name]["cmds"], width=10**6)

    def test_memory_limit_is_configured(self, taskfile: dict) -> None:
        assert "SANDBOX_BUILD_MEMORY" in taskfile["vars"]
        assert "2g" in taskfile["vars"]["SANDBOX_BUILD_MEMORY"]

    def test_builder_task_sets_a_memory_limit(self, taskfile: dict) -> None:
        cmds = self._cmds(taskfile, BUILDER_TASK)

        assert "--driver docker-container" in cmds
        assert "--driver-opt memory={{.SANDBOX_BUILD_MEMORY}}" in cmds

    def test_builder_is_recreated_when_the_limit_differs(self, taskfile: dict) -> None:
        """A running buildkit container cannot be given a new limit - it must be replaced."""
        cmds = self._cmds(taskfile, BUILDER_TASK)

        assert "docker buildx rm" in cmds

    def test_builder_is_created_when_this_session_does_not_know_it(self, taskfile: dict) -> None:
        """`docker buildx build --builder` reads the session's buildx store, not the daemon.

        A container in the daemon says nothing about this session: every dclaude session on
        the shared server has its own ~/.docker/buildx. Checking only `docker inspect` makes
        the task report "staat klaar" after which the build dies on 'no builder found'.
        """
        # raw, niet via safe_dump: die escapet de aanhalingstekens in de shell-conditie
        cmds = "\n".join(str(cmd) for cmd in taskfile["tasks"][BUILDER_TASK]["cmds"])

        assert (
            'if ! docker buildx inspect {{.SANDBOX_BUILDER_NAME}} >/dev/null 2>&1 || [ "$STATE" != "$WANT host" ]; then'
            in cmds
        )

    def test_build_task_runs_the_preflight_check_first(self, taskfile: dict) -> None:
        cmds = taskfile["tasks"][BUILD_TASK]["cmds"]

        assert cmds[0] == "scripts/build-preflight.sh"

    def test_build_task_uses_the_bounded_builder(self, taskfile: dict) -> None:
        task = taskfile["tasks"][BUILD_TASK]

        assert {"task": BUILDER_TASK} in task["cmds"]
        assert "--builder {{.SANDBOX_BUILDER_NAME}}" in self._cmds(taskfile, BUILD_TASK)

    def test_build_task_passes_cache_flags(self, taskfile: dict) -> None:
        """Without a cache every build redoes the three apt rounds in the Dockerfile."""
        cmds = self._cmds(taskfile, BUILD_TASK)

        assert "--cache-from" in cmds
        assert "SANDBOX_CACHE_IMAGE" in cmds

    def test_update_task_delegates_the_build(self, taskfile: dict) -> None:
        """One build path only: a copied 'docker buildx build' drifts away from the guards."""
        task = taskfile["tasks"][UPDATE_TASK]

        assert {"task": BUILD_TASK} in task["cmds"]
        assert "docker buildx build" not in self._cmds(taskfile, UPDATE_TASK)
