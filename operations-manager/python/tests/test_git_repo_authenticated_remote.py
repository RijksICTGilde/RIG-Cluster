"""GitRepo against a remote that demands credentials, as Forgejo and GitHub do.

The copy is a real blobless clone here: a remote given as a plain path is cloned with all
blobs (git ignores ``--filter`` for a local clone), so a missing blob never shows up there.
A git command that reaches the remote without credentials, such as a lazy fetch of a
missing blob, gets a 401 here and fails, as it did on the sandbox.
"""

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import FileChange
from opi.connectors.git_repo import GitCredentials, GitRepo, RepoAt, get_git_repo, reset_git_repos
from tests import git_http_remote

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.skipif(git_http_remote.http_backend() is None, reason="git-http-backend not available")

GIT = shutil.which("git") or "/usr/bin/git"
CREDENTIALS = GitCredentials(username=git_http_remote.USERNAME, password=git_http_remote.PASSWORD)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run([GIT, *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def _own_copy_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import opi.connectors.git_repo as module

    monkeypatch.setattr(module, "GIT_REPOS_DIR", str(tmp_path / "git-repos"))
    reset_git_repos()
    yield
    reset_git_repos()


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """The deployments repo in miniature: a file in the root, a cluster folder, two projects."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "README.md").write_text("root file\n")
    for project in ("alfa", "beta"):
        folder = seed / "prd" / project
        folder.mkdir(parents=True)
        (folder / "kustomization.yaml").write_text(f"name: {project}\n")
        (folder / "deployment.yaml").write_text(f"app: {project}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "first")

    root = tmp_path / "served"
    root.mkdir()
    repo = root / "deployments.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(repo))
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(repo, "config", "http.receivepack", "true")
    return repo


@pytest.fixture
def served(bare: Path) -> Iterator[tuple[str, list[tuple[str, str, bool]]]]:
    with git_http_remote.serve(bare.parent) as (base_url, requests):
        yield f"{base_url}/{bare.name}", requests


def _repo(url: str) -> GitRepo:
    return get_git_repo(url, "main")


def _commit_outside(bare: Path, tmp_path: Path, path: str, content: str) -> str:
    """A manual commit straight on the remote, outside ZAD."""
    outside = tmp_path / f"outside-{path.replace('/', '-')}"
    _git(tmp_path, "clone", "-q", str(bare), str(outside))
    (outside / path).parent.mkdir(parents=True, exist_ok=True)
    (outside / path).write_text(content)
    _git(outside, "add", "-A")
    _git(outside, "commit", "-qm", f"outside: {path}")
    _git(outside, "push", "-q", "origin", "main")
    return _git(bare, "rev-parse", "main")


def _blob_missing(repo: GitRepo, bare: Path, path: str) -> bool:
    oid = _git(bare, "rev-parse", f"main:{path}")
    done = subprocess.run(
        [GIT, "cat-file", "-e", oid],
        cwd=repo.path,
        env={**_GIT_ENV, "GIT_NO_LAZY_FETCH": "1"},
        capture_output=True,
        check=False,
    )
    return done.returncode != 0


def _show(bare: Path, path: str) -> str:
    return _git(bare, "show", f"main:{path}")


async def _write_dir(repo: GitRepo, tmp_path: Path, files: dict[str, str]) -> None:
    """A rollout of project alfa: materialize its folder, change files, write it back."""
    work = tmp_path / "work"
    shutil.rmtree(work, ignore_errors=True)
    base = await repo.tip(CREDENTIALS)
    await repo.materialize(["prd/alfa"], base, str(work), CREDENTIALS)
    for path, content in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(content)
    result = await repo.write_dir(
        base_ref=base,
        scope_paths=["prd/alfa"],
        source_dir=str(work),
        message="rollout alfa",
        actor=None,
        credentials=CREDENTIALS,
    )
    assert result.committed


def _assert_all_authorized(requests: list[tuple[str, str, bool]]) -> None:
    unauthorized = [(method, path) for method, path, authorized in requests if not authorized]
    assert unauthorized == []


@pytest.mark.asyncio
async def test_the_copy_is_really_blobless(served: tuple[str, list], bare: Path) -> None:
    url, _ = served
    repo = _repo(url)
    await repo.tip(CREDENTIALS)

    assert _blob_missing(repo, bare, "README.md")
    assert _blob_missing(repo, bare, "prd/beta/deployment.yaml")


@pytest.mark.asyncio
async def test_an_outside_commit_to_a_root_file_does_not_break_the_next_write(
    served: tuple[str, list], bare: Path, tmp_path: Path
) -> None:
    """The sandbox failure: the root has a blob the copy never fetched, and the write
    rebuilds the root tree on the retry after the rejected push."""
    url, requests = served
    repo = _repo(url)
    await repo.tip(CREDENTIALS)
    outside = _commit_outside(bare, tmp_path, "README.md", "changed outside\n")

    await _write_dir(repo, tmp_path, {"prd/alfa/deployment.yaml": "app: alfa\nreplicas: 2\n"})

    assert outside in _git(bare, "rev-list", "main")
    assert _show(bare, "README.md") == "changed outside"
    assert _show(bare, "prd/alfa/deployment.yaml") == "app: alfa\nreplicas: 2"
    _assert_all_authorized(requests)


@pytest.mark.asyncio
async def test_an_outside_commit_in_the_own_project_folder_survives_our_write(
    served: tuple[str, list], bare: Path, tmp_path: Path
) -> None:
    """Our write builds on the outside commit: its file in our folder is the remote's state."""
    url, requests = served
    repo = _repo(url)
    await repo.tip(CREDENTIALS)
    _commit_outside(bare, tmp_path, "prd/alfa/manual.yaml", "manual: true\n")

    await repo.sync(CREDENTIALS)
    await _write_dir(repo, tmp_path, {"prd/alfa/deployment.yaml": "app: alfa\nreplicas: 3\n"})

    assert _show(bare, "prd/alfa/manual.yaml") == "manual: true"
    assert _show(bare, "prd/alfa/deployment.yaml") == "app: alfa\nreplicas: 3"
    _assert_all_authorized(requests)


@pytest.mark.asyncio
async def test_a_commit_on_a_fresh_copy_keeps_the_untouched_neighbours(served: tuple[str, list], bare: Path) -> None:
    """``commit`` without a workspace: the neighbours of the changed file were never fetched."""
    url, requests = served
    repo = _repo(url)

    async def change(_: RepoAt) -> dict[str, FileChange | None]:
        return {"prd/alfa/deployment.yaml": FileChange(content=b"app: alfa\nreplicas: 4\n")}

    await repo.tip(CREDENTIALS)
    after_clone = len(requests)
    result = await repo.commit(change, message="commit alfa", actor=None, credentials=CREDENTIALS)

    assert result.committed
    # Only the push itself: no lazy fetch of a missing blob, not even with credentials.
    assert [(method, path.split(".git/")[-1]) for method, path, _ in requests[after_clone:]] == [
        ("GET", "info/refs?service=git-receive-pack"),
        ("POST", "git-receive-pack"),
    ]
    assert _show(bare, "prd/alfa/kustomization.yaml") == "name: alfa"
    assert _show(bare, "README.md") == "root file"
    assert _show(bare, "prd/alfa/deployment.yaml") == "app: alfa\nreplicas: 4"
    _assert_all_authorized(requests)


@pytest.mark.asyncio
async def test_read_file_on_a_fresh_copy_returns_the_content(served: tuple[str, list]) -> None:
    url, requests = served
    repo = _repo(url)
    tip = await repo.tip(CREDENTIALS)

    assert await repo.read_file("prd/beta/deployment.yaml", tip, CREDENTIALS) == b"app: beta\n"
    assert await repo.read_file("prd/beta/absent.yaml", tip, CREDENTIALS) is None
    _assert_all_authorized(requests)


@pytest.mark.asyncio
async def test_read_file_that_cannot_fetch_the_content_raises_instead_of_reporting_absent(
    served: tuple[str, list],
) -> None:
    """Without credentials the blob cannot come in; that is not the same as "not there"."""
    url, _ = served
    repo = _repo(url)
    tip = await repo.tip(CREDENTIALS)

    with pytest.raises(RuntimeError):
        await repo.read_file("prd/beta/deployment.yaml", tip, GitCredentials())


@pytest.mark.asyncio
async def test_materialize_on_a_fresh_copy_brings_the_scope(served: tuple[str, list], tmp_path: Path) -> None:
    url, requests = served
    repo = _repo(url)
    tip = await repo.tip(CREDENTIALS)
    target = tmp_path / "target"

    await repo.materialize(["prd/beta"], tip, str(target), CREDENTIALS)

    assert (target / "prd" / "beta" / "deployment.yaml").read_text() == "app: beta\n"
    _assert_all_authorized(requests)
