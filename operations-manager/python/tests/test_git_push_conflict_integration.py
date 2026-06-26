"""Real-git end-to-end test for the project-file push self-heal (1b).

Unlike test_git_push_conflict.py (which mocks the git layer to check control
flow), this drives actual git against a local bare remote: it forces a real
rebase conflict and proves the reapply path resets to the remote, re-applies the
intended change on fresh content, and converges, while preserving the concurrent
writer's independent change.

The GitConnector only understands ssh/http(s)/git URLs, so the test pre-clones
the bare remote into the connector's working_dir and skips the connector's own
clone step. The push/fetch/rebase/reset code under test operates on the working
tree's configured ``origin`` remote, exactly as in production.
"""

import subprocess
from typing import TYPE_CHECKING

from opi.connectors.git import GitConnector

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit_push(repo: Path, content: str, message: str) -> None:
    (repo / "data.txt").write_text(content)
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", message, cwd=repo)
    _git("push", "origin", "main", cwd=repo)


def _set_identity(repo: Path) -> None:
    _git("config", "user.email", "test@test", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)


async def test_self_heal_converges_and_preserves_concurrent_change(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)

    # Seed the remote with two independent lines.
    seed = tmp_path / "seed"
    _git("clone", str(remote), str(seed), cwd=tmp_path)
    _set_identity(seed)
    _commit_push(seed, "a=base\nb=base\n", "seed")

    # The connector's working tree: a separate clone of the same remote.
    working = tmp_path / "working"
    _git("clone", str(remote), str(working), cwd=tmp_path)
    _set_identity(working)

    connector = GitConnector(repo_url="https://example.com/repo.git", branch="main", working_dir=str(working))
    # Skip the connector's own clone/fetch; the working tree is already set up.
    connector._repo_cloned = True
    connector._fetched_in_session = True

    # Stage our intended change in the working tree: a=ours.
    (working / "data.txt").write_text("a=ours\nb=base\n")

    # A concurrent writer changes BOTH lines and pushes first, so our push is
    # rejected and the rebase of "a=ours" onto "a=remote" conflicts on line a.
    _commit_push(seed, "a=remote\nb=remote\n", "concurrent writer")

    async def reapply() -> None:
        # Re-read whatever is now on disk (the reset put a=remote, b=remote there)
        # and re-apply only our intent (a=ours), leaving b untouched.
        lines = (working / "data.txt").read_text().splitlines()
        rewritten = ["a=ours" if line.startswith("a=") else line for line in lines]
        (working / "data.txt").write_text("\n".join(rewritten) + "\n")

    # Must not raise: the conflict is recovered via reset + reapply + retry.
    await connector.commit_and_push("set a=ours", reapply=reapply)

    # Verify the remote ended in the converged state.
    verify = tmp_path / "verify"
    _git("clone", str(remote), str(verify), cwd=tmp_path)
    final = (verify / "data.txt").read_text()
    assert "a=ours" in final, f"our intent must win on the conflicting line: {final!r}"
    assert "b=remote" in final, f"concurrent independent change must be preserved: {final!r}"
