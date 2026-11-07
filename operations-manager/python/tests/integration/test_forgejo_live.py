"""OPI's git-laag tegen de Forgejo die het platform draait.

Elke methode van ``GitConnector`` en ``GitRepo`` die ``opi/`` aanroept, draait hier ten
minste een keer, tegen een echte Forgejo in de gepinde versie. Het terugleeskanaal is de
API van Forgejo (``ForgejoApi``), niet de connector onder toets: wat een push achterlaat,
beslist de server.

Wat deze groep meet en de kale remote niet: een partial clone (oudere Forgejo kan dat niet,
en dan valt de connector terug op een volledige clone), een push die Forgejo weigert, en de
authenticatie met een token.

Draai hem met ``task test-forgejo``; zonder ``FORGEJO_LIVE_URL`` slaat de groep zichzelf
over. ``tests/test_live_group_coverage.py`` bewaakt dat er geen methode ontbreekt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import FileChange, GitConnector, GitPushRejectedError
from opi.connectors.git_repo import (
    GitCredentials,
    get_git_repo,
    reset_git_repos,
    warm_platform_repos,
)
from opi.core.config import settings
from tests.integration.forgejo_live import PLATFORM_REPOS, ForgejoApi, admin_user, assert_local

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from opi.connectors.git_repo import GitRepo, RepoAt

pytestmark = [pytest.mark.requires_infra, pytest.mark.forgejo_live]

#: Elke repository die deze groep maakt begint hiermee.
PREFIX = "zadlive"
REPO = f"{PREFIX}-deployments"

#: Het projectbestand van een project, op de plek waar de projects-repo het bewaart.
PROJECT_FILE = "projects/alfa.yaml"


@pytest.fixture(scope="module")
def api() -> Iterator[ForgejoApi]:
    assert_local()
    opstelling = ForgejoApi()
    yield opstelling
    opstelling.drop_token()


@pytest.fixture
def repo_url(api: ForgejoApi) -> Iterator[str]:
    """Een verse repository met een projectbestand en een map per project."""
    url = api.fresh_repo(
        REPO,
        files={
            PROJECT_FILE: "name: alfa\nversion: 1\n",
            "prd/alfa/deployment.yaml": "app: alfa\n",
            "prd/beta/deployment.yaml": "app: beta\n",
        },
    )
    yield url
    api.drop_repo(REPO)


@pytest.fixture
def credentials(api: ForgejoApi) -> GitCredentials:
    return GitCredentials(username=admin_user(), password=api.token)


def connector(url: str, api: ForgejoApi, *, full_history: bool = False) -> GitConnector:
    """De connector zoals OPI hem bouwt: HTTPS met een token als wachtwoord."""
    return GitConnector(
        repo_url=url,
        branch="main",
        username=admin_user(),
        password=f"plain:{api.token}",
        name="zadlive",
        full_history=full_history,
    )


async def test_a_clone_with_a_token_gets_the_repository(repo_url: str, api: ForgejoApi) -> None:
    git = connector(repo_url, api)
    try:
        await git.ensure_repo_cloned()
        assert await git.read_file(PROJECT_FILE) == "name: alfa\nversion: 1\n"
        assert await git.get_local_commit_hash() == api.tip(REPO)
        assert await git.get_remote_commit_hash() == api.tip(REPO)
        assert await git.get_working_dir()
    finally:
        await git.close()


async def test_a_clone_without_credentials_is_refused(repo_url: str, api: ForgejoApi) -> None:
    """De repositories zijn prive; een clone zonder token hoort niet te lukken."""
    git = GitConnector(repo_url=repo_url, branch="main", name="zadlive-anoniem")
    try:
        with pytest.raises(RuntimeError, match=r"(?i)auth|credential|403|401|not found|terminal prompts"):
            await git.clone()
    finally:
        await git.close()


async def test_a_partial_clone_carries_the_history_forgejo_has(repo_url: str, api: ForgejoApi) -> None:
    """``--filter=blob:none``: de connector valt terug op een volledige clone als Forgejo het niet kan."""
    base = api.tip(REPO)
    api.write_file(REPO, PROJECT_FILE, "name: alfa\nversion: 2\n", message="bump")
    git = connector(repo_url, api, full_history=True)
    try:
        await git.ensure_repo_cloned()
        assert await git.read_file(PROJECT_FILE) == "name: alfa\nversion: 2\n"
        # Zonder de laatste regelovergang: dit komt uit de uitvoer van `git show`.
        assert await git.get_previous_file_content(PROJECT_FILE) == "name: alfa\nversion: 1"
        revisions = await git.list_file_revisions(PROJECT_FILE)
        assert [revision["message"] for revision in revisions] == ["bump", f"add {PROJECT_FILE}"]
        # Sinds de commit voor de bump: het projectbestand veranderde, de map van beta niet.
        assert await git.path_unchanged_since(base, "prd/beta") is True
        assert await git.path_unchanged_since(base, PROJECT_FILE) is False
    finally:
        await git.close()


async def test_a_commit_and_push_lands_on_forgejo(repo_url: str, api: ForgejoApi) -> None:
    git = connector(repo_url, api)
    try:
        await git.ensure_repo_cloned()
        before = await git.get_local_commit_hash()

        await git.write_file_without_commit(PROJECT_FILE, "name: alfa\nversion: 3\n")
        assert await git.count_unpushed_commits() == 0
        await git.commit_and_push("zadlive: bump naar 3")

        assert api.file_content(REPO, PROJECT_FILE) == "name: alfa\nversion: 3\n"
        # OPI zet een Actor-regel onder elk bericht, dus de vergelijking gaat op de eerste regel.
        assert any(message.startswith("zadlive: bump naar 3") for message in api.commit_messages(REPO))
        after = api.tip(REPO)
        assert after != before
        assert await git.get_remote_commit_hash() == after
        assert await git.count_unpushed_commits() == 0
        assert await git.list_changed_files(before, after) == [PROJECT_FILE]

        sha = await git.get_blob_sha(PROJECT_FILE)
        assert sha is not None
        assert await git.read_blob(sha) == "name: alfa\nversion: 3"
        # show_file_at geeft de uitvoer van `git show` terug, en die eindigt zonder de
        # laatste regelovergang; read_file leest het bestand zelf en houdt hem.
        assert await git.show_file_at(before, PROJECT_FILE) == "name: alfa\nversion: 1"
        assert set(await git.last_modified_per_file("prd")) == {
            "prd/alfa/deployment.yaml",
            "prd/beta/deployment.yaml",
        }
    finally:
        await git.close()


async def test_a_push_forgejo_refuses_raises_instead_of_reporting_success(repo_url: str, api: ForgejoApi) -> None:
    """Een beschermde branch: de weigering komt van de server, niet van git zelf."""
    git = connector(repo_url, api)
    try:
        await git.ensure_repo_cloned()
        api.protect_branch(REPO)
        before = api.tip(REPO)

        await git.write_file_without_commit(PROJECT_FILE, "name: alfa\nversion: 9\n")
        with pytest.raises(GitPushRejectedError):
            await git.commit_and_push("zadlive: mag niet")

        assert api.tip(REPO) == before
        assert api.file_content(REPO, PROJECT_FILE) == "name: alfa\nversion: 1\n"
    finally:
        await git.close()


async def test_a_commit_built_without_a_worktree_lands_on_the_branch(repo_url: str, api: ForgejoApi) -> None:
    """De weg die de generatie gebruikt: een commit bouwen, de branch verzetten, pushen."""
    git = connector(repo_url, api)
    try:
        await git.ensure_repo_cloned()
        commit = await git.build_commit(
            {"prd/alfa/deployment.yaml": "app: alfa\nreplicas: 2\n", "prd/beta/deployment.yaml": None},
            message="zadlive: alfa omhoog, beta weg",
        )
        assert commit is not None
        await git.set_branch_ref(commit)
        await git.sync_worktree_to_head()
        await git.push_changes()

        assert api.file_content(REPO, "prd/alfa/deployment.yaml") == "app: alfa\nreplicas: 2\n"
        assert api.file_content(REPO, "prd/beta/deployment.yaml") is None
        assert api.tip(REPO) == commit
    finally:
        await git.close()


async def test_reset_to_remote_throws_away_what_was_never_pushed(repo_url: str, api: ForgejoApi) -> None:
    git = connector(repo_url, api)
    try:
        await git.ensure_repo_cloned()
        remote = api.write_file(REPO, PROJECT_FILE, "name: alfa\nversion: 4\n", message="buiten OPI om")

        await git.write_file_without_commit(PROJECT_FILE, "name: alfa\nversion: eigen\n")
        await git.reset_to_remote()

        assert await git.read_file(PROJECT_FILE) == "name: alfa\nversion: 4\n"
        assert await git.get_local_commit_hash() == remote
    finally:
        await git.close()


# ==================== De beheerde kopie (GitRepo) ====================


@pytest.fixture(autouse=True)
def own_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Een eigen map voor de kopieen, zodat een toets niets van een andere ziet."""
    monkeypatch.setattr("opi.connectors.git_repo.GIT_REPOS_DIR", str(tmp_path / "git-repos"))
    reset_git_repos()
    yield
    reset_git_repos()


async def test_the_managed_copy_reads_and_writes_against_forgejo(
    repo_url: str, api: ForgejoApi, credentials: GitCredentials
) -> None:
    copy: GitRepo = get_git_repo(repo_url, "main")
    tip = await copy.tip(credentials)
    assert tip == api.tip(REPO)
    assert await copy.resolve(credentials) == tip
    assert await copy.read_file(PROJECT_FILE, tip, credentials) == b"name: alfa\nversion: 1\n"
    assert set(await copy.list_tree("prd", tip)) >= {
        "prd/alfa/deployment.yaml",
        "prd/beta/deployment.yaml",
    }

    async def change(_: RepoAt) -> dict[str, FileChange | None]:
        return {PROJECT_FILE: FileChange(content=b"name: alfa\nversion: 5\n")}

    result = await copy.commit(change, message="zadlive: via de kopie", actor=None, credentials=credentials)
    assert result.committed
    assert api.file_content(REPO, PROJECT_FILE) == "name: alfa\nversion: 5\n"
    assert any(message.startswith("zadlive: via de kopie") for message in api.commit_messages(REPO))

    # Een commit buiten OPI om: fetch en sync halen hem binnen, path_unchanged_since ziet
    # dat de eigen map er niet in zat.
    before = await copy.tip(credentials)
    api.write_file(REPO, "prd/beta/deployment.yaml", "app: beta\nreplicas: 3\n", message="buiten OPI om")
    await copy.fetch(credentials)
    await copy.sync(credentials)
    after = await copy.tip(credentials)
    assert after == api.tip(REPO) != before
    assert await copy.path_unchanged_since(before, "prd/alfa", after) is True
    assert await copy.path_unchanged_since(before, "prd/beta", after) is False


async def test_materialize_writes_only_the_paths_of_the_scope(
    repo_url: str, api: ForgejoApi, credentials: GitCredentials, tmp_path: Path
) -> None:
    copy: GitRepo = get_git_repo(repo_url, "main")
    tip = await copy.tip(credentials)
    work = tmp_path / "werkmap"
    await copy.materialize(["prd/alfa"], tip, str(work), credentials)

    assert (work / "prd/alfa/deployment.yaml").read_text() == "app: alfa\n"
    assert not (work / "prd/beta").exists()


async def test_warm_platform_repos_makes_a_copy_of_the_platform_repos(
    api: ForgejoApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De opstartstap: de kopieen staan klaar voordat de eerste taak erom vraagt."""
    urls = {name: api.fresh_repo(f"{PREFIX}-{name}", files={"README.md": name}) for name in PLATFORM_REPOS}
    try:
        monkeypatch.setattr(settings, "PROJECT_REPO_URL", urls["zad-deployments"])
        monkeypatch.setattr(settings, "PROJECT_REPO_BRANCH", "main")
        monkeypatch.setattr(settings, "PROJECT_REPO_USERNAME", admin_user())
        monkeypatch.setattr(settings, "PROJECT_REPO_PASSWORD", f"plain:{api.token}")
        monkeypatch.setattr(settings, "GIT_ARGO_APPLICATIONS_URL", urls["zad-argo-user-applications"])
        monkeypatch.setattr(settings, "GIT_ARGO_APPLICATIONS_BRANCH", "main")
        monkeypatch.setattr(settings, "GIT_ARGO_APPLICATIONS_USERNAME", admin_user())
        monkeypatch.setattr(settings, "GIT_ARGO_APPLICATIONS_PASSWORD", f"plain:{api.token}")

        await warm_platform_repos()

        credentials = GitCredentials(username=admin_user(), password=api.token)
        for name in ("zad-deployments", "zad-argo-user-applications"):
            copy: GitRepo = get_git_repo(urls[name], "main")
            assert await copy.tip(credentials) == api.tip(f"{PREFIX}-{name}")
    finally:
        for name in PLATFORM_REPOS:
            api.drop_repo(f"{PREFIX}-{name}")
