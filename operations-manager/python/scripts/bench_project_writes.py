"""Measure where the time goes when many actions hit one project file.

Answers question 1 and 2 of the RC-117 research plan for the *project-file* half of
an action: for N sequential saves on one project, how much time goes to acquiring the
store lock, validating, building the commit, pushing, syncing the worktree, and
reconstructing the previous version for the prune diff.

It drives the real ``GitProjectStore`` against a real git repository (a local bare
remote), so the git costs are git's own and not a fake's. What it deliberately does
NOT include is the network: a local bare remote pushes over the filesystem, so the
push number here is the floor. The sandbox run in
``docs/rc117-veel-acties-meting.md`` adds the real network and the rollout.

Usage (from operations-manager/python):

    uv run python scripts/bench_project_writes.py --actions 10
    uv run python scripts/bench_project_writes.py --actions 10 --projects 200 --history 2000

``--projects`` seeds that many other project files and ``--history`` that many extra
commits, so the scaling question ("does this get worse as the repo grows?") is
answered by running it twice rather than argued about.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opi.connectors.git import GitConnector  # noqa: E402
from opi.services.project_service import get_project_service  # noqa: E402
from opi.services.project_store import GitProjectStore  # noqa: E402
from opi.utils.yaml_util import dump_yaml_to_string  # noqa: E402
from scripts.bench_support import PhaseTimer, format_table  # noqa: E402

# Some containers export an empty GIT_AUTHOR_NAME, which takes precedence over git
# config and makes `git commit` fail with "empty ident name". Pin a full identity so
# the benchmark never depends on the ambient one.
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@example.com",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@example.com",
}

PROJECT_NAME = "bench"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_GIT_ENV
    )
    return result.stdout.strip()


def project_data(name: str, components: int = 0, marker: int = 0) -> dict[str, Any]:
    """A minimal project file that passes schema + structural validation.

    ``marker`` only varies the description, so a seeding loop can guarantee every
    commit changes something without changing the file's shape.
    """
    return {
        "schema-version": 2,
        "name": name,
        "display-name": name,
        "description": f"benchmark project {marker}",
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["odcn-production"],
        "services": [],
        "components": [
            {
                "name": f"component-{i}",
                "type": "single",
                "ports": {"inbound": [8000], "outbound": [80, 443]},
                "path": [{"match": f"/component-{i}"}],
            }
            for i in range(components)
        ],
        "deployments": [],
        "config": {"api-key": "base64+age:YmVuY2gtYXBpLWtleQ=="},
    }


def seed_remote(root: Path, *, projects: int, history: int) -> Path:
    """Build a bare remote holding ``projects`` project files and ``history`` commits.

    History is added as commits that touch OTHER project files, which is what the
    shared projects repo actually looks like: the previous version of one project's
    file is many commits back, so a file-scoped log has to walk past all of them.
    """
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "projects").mkdir()

    (seed / "projects" / f"{PROJECT_NAME}.yaml").write_text(dump_yaml_to_string(project_data(PROJECT_NAME)))
    for i in range(projects):
        (seed / "projects" / f"other-{i}.yaml").write_text(dump_yaml_to_string(project_data(f"other-{i}")))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")

    for i in range(history):
        target = seed / "projects" / (f"other-{i % projects}.yaml" if projects else f"{PROJECT_NAME}.yaml")
        data = project_data(target.stem, components=i % 5, marker=i + 1)
        target.write_text(dump_yaml_to_string(data))
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", f"history {i}")

    bare = root / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)], check=True, capture_output=True, env=_GIT_ENV
    )
    return bare


class TimingConnector:
    """Proxies a real GitConnector, timing the calls the write path makes.

    A wrapper rather than an edit to the store: the thing being measured has to be
    the code that runs in production, not a copy of it with timers in it.
    """

    _TIMED = {
        "count_unpushed_commits",
        "build_commit",
        "set_branch_ref",
        "push_changes",
        "sync_worktree_to_head",
        "get_local_commit_hash",
        "get_previous_file_content",
        "show_file_at",
    }

    def __init__(self, inner: GitConnector, timer: PhaseTimer) -> None:
        self._inner = inner
        self._timer = timer

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._inner, item)
        if item not in self._TIMED or not callable(attr):
            return attr

        async def timed(*args: Any, **kwargs: Any) -> Any:
            with self._timer.measure(f"git.{item}"):
                return await attr(*args, **kwargs)

        return timed


async def run(actions: int, projects: int, history: int) -> int:
    root = Path(tempfile.mkdtemp(prefix="bench-writes-"))
    timer = PhaseTimer()
    try:
        print(f"seeding remote: {projects} other projects, {history} history commits ...", flush=True)
        seeded = time.monotonic()
        bare = seed_remote(root, projects=projects, history=history)
        print(f"seeded in {time.monotonic() - seeded:.1f}s", flush=True)

        working_dir = str(root / "warm")
        # GitConnector only parses git://, ssh:// and http(s):// URLs, so the warm copy
        # is cloned here with the same flags the real project-files connector uses
        # (--filter=blob:none, full history) and the connector is pointed at the result.
        # Everything after this point -- commit, push, log, show -- is the real code path.
        with timer.measure("clone (once per process)"):
            subprocess.run(
                ["git", "clone", "-q", "--filter=blob:none", "-b", "main", str(bare), working_dir],
                check=True,
                capture_output=True,
                env=_GIT_ENV,
            )
        connector = GitConnector(
            repo_url="https://example.invalid/zad-projects.git",
            working_dir=working_dir,
            branch="main",
            full_history=True,
            name="bench",
        )
        connector._repo_cloned = True
        connector._fetched_in_session = True

        store = GitProjectStore(working_dir=working_dir)
        store._connector = TimingConnector(connector, timer)  # type: ignore[assignment]
        with timer.measure("bootstrap (once per process)"):
            await store.bootstrap()

        project = get_project_service().get_project(PROJECT_NAME)
        if project is None:
            print(f"ERROR: bootstrap did not load '{PROJECT_NAME}'", file=sys.stderr)
            return 1

        startup = {name: timer.stats(name) for name in ("clone (once per process)", "bootstrap (once per process)")}
        # Bootstrap reads every project file, so its git calls would dominate the
        # per-action table and answer a different question. The startup cost is
        # reported separately above; the table below is the action loop only.
        timer.samples.clear()

        print(f"running {actions} sequential saves on one project ...", flush=True)
        run_started = time.monotonic()
        for i in range(actions):
            data = project_data(PROJECT_NAME, components=i + 1)
            with timer.measure("save() end to end"):
                await store.save(PROJECT_NAME, data, message=f"bench action {i}", actor="bench")
            # The prune diff reconstructs the previous version after every action.
            with timer.measure("previous() for the prune diff"):
                await store.previous(PROJECT_NAME)
        wall = time.monotonic() - run_started

        end_to_end = timer.stats("save() end to end")
        previous = timer.stats("previous() for the prune diff")
        git_phases = [s for s in timer.all_stats() if s.name.startswith("git.")]

        print()
        print("paid once per process, not per action:")
        for name, stat in startup.items():
            print(f"  {name:<28}: {stat.total:.3f}s")
        print()
        print(f"{actions} actions on one project: {wall:.2f}s wall clock")
        print(f"  save() end to end : {end_to_end.total:.3f}s total, {end_to_end.mean:.3f}s mean")
        print(f"  previous()        : {previous.total:.3f}s total, {previous.mean:.3f}s mean")
        print()
        print("git calls inside the write path (nested inside save()):")
        print(format_table(git_phases, total_label="git call"))
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", type=int, default=10, help="sequential saves on one project (default 10)")
    parser.add_argument("--projects", type=int, default=50, help="other project files in the repo (default 50)")
    parser.add_argument("--history", type=int, default=200, help="extra commits before the run (default 200)")
    args = parser.parse_args()
    return asyncio.run(run(args.actions, args.projects, args.history))


if __name__ == "__main__":
    raise SystemExit(main())
