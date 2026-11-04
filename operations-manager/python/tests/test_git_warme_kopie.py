"""Tests voor de warme werkkopie waar elke taak lokaal uit kloont.

De deployments- en argo-repo's werden per taak drie keer vers over het netwerk gekloond
(RC-418). Er staat nu een warme blobless kopie per (repo, branch) die blijft staan en met
``fetch`` wordt bijgewerkt; elke taak kloont daar LOKAAL van naar zijn eigen werkmap.

Deze tests draaien tegen echte git-repo's op schijf, want de drie dingen die hier mis
kunnen gaan zijn alle drie git-gedrag: dat twee taken nooit dezelfde working tree delen,
dat een diff per bestand nog historie heeft (de regressie van issue #159), en dat een halve
kopie zich herstelt.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import (
    GitConnector,
    _remote_default_branches,
    _warm_cache_locks,
    _warm_cache_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

GIT = shutil.which("git") or "/usr/bin/git"

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


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Een repo met drie commits waarin hetzelfde bestand twee keer wijzigt.

    ``uploadpack.allowFilter`` staat aan: zonder dat weigert de "server" een blobless
    kloon, en dan meet de test niet wat productie doet.
    """
    zaad = tmp_path / "zaad"
    zaad.mkdir()
    _git(zaad, "init", "-q", "-b", "main", ".")
    for inhoud in ("een", "twee", "drie"):
        (zaad / "waarde.txt").write_text(inhoud)
        (zaad / f"{inhoud}.txt").write_text(inhoud)
        _git(zaad, "add", "-A")
        _git(zaad, "commit", "-qm", f"commit {inhoud}")

    # Bare, zoals de echte remote: een repo met main uitgechecked weigert een push.
    pad = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", "-b", "main", str(zaad), str(pad))
    _git(pad, "config", "uploadpack.allowFilter", "true")
    return pad


def _nieuwe_commit_op_de_remote(remote: Path, naam: str, inhoud: str) -> None:
    """Leg een commit op de bare remote, zoals een andere taak dat zou doen."""
    _git(remote, "clone", "-q", "-b", "main", str(remote), str(remote.parent / "duwer"))
    duwer = remote.parent / "duwer"
    (duwer / naam).write_text(inhoud)
    _git(duwer, "add", "-A")
    _git(duwer, "commit", "-qm", f"commit {naam}")
    _git(duwer, "push", "-q", "origin", "main")
    shutil.rmtree(duwer)


@pytest.fixture(autouse=True)
def _schone_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Zet de warme kopie in de tmp van deze test, niet in die van het cluster."""
    cache_dir = tmp_path / "git-warm"
    monkeypatch.setattr("opi.connectors.git.WARM_CACHE_DIR", str(cache_dir))
    _warm_cache_locks.clear()
    _remote_default_branches.clear()
    yield
    _warm_cache_locks.clear()
    _remote_default_branches.clear()


#: De URL waarmee de connector wordt gebouwd. GitConnector parseert alleen git://, ssh://
#: en http(s)://, dus de repo op schijf komt er na de constructie in, op precies de plek die
#: git als argument krijgt (``repo_url_with_path``). Zo loopt de kloonweg zelf echt.
REPO_URL = "https://example.invalid/zad-deployments.git"


def _connector(remote: Path, *, working_dir: str | None = None, url: str = REPO_URL) -> GitConnector:
    """Een connector zoals een taak hem krijgt: eigen wegwerpwerkmap, dus warme kopie."""
    connector = GitConnector(
        repo_url=url, branch="main", name="deployments", working_dir=working_dir, full_history=working_dir is not None
    )
    connector._clean_repo_url = str(remote)
    return connector


@pytest.mark.asyncio
async def test_de_werkmap_komt_uit_de_warme_kopie(remote: Path, tmp_path: Path) -> None:
    connector = _connector(remote)
    await connector.ensure_repo_cloned()

    werkmap = Path(await connector.get_working_dir())
    assert (werkmap / "waarde.txt").read_text() == "drie"
    assert os.path.isdir(os.path.join(_warm_cache_path(REPO_URL, "main"), ".git"))
    # Geen alternates: de werkmap leent geen objecten, ze zijn gehardlinkt. Zo kan een gc
    # in de kopie nooit een object onder een lopende taak weghalen.
    assert not (werkmap / ".git" / "objects" / "info" / "alternates").exists()
    await connector.close()


@pytest.mark.asyncio
async def test_origin_wijst_na_de_lokale_kloon_op_de_echte_remote(remote: Path) -> None:
    connector = _connector(remote)
    await connector.ensure_repo_cloned()
    werkmap = Path(await connector.get_working_dir())

    assert _git(werkmap, "remote", "get-url", "origin") == str(remote)
    await connector.close()


@pytest.mark.asyncio
async def test_een_diff_tegen_een_oude_commit_haalt_de_ontbrekende_blob_op(remote: Path) -> None:
    """De regressie van issue #159: zonder historie en zonder promisor vindt die diff niets."""
    connector = _connector(remote)
    await connector.ensure_repo_cloned()
    werkmap = Path(await connector.get_working_dir())

    diff = _git(werkmap, "diff", "HEAD~2", "HEAD", "--", "waarde.txt")
    assert "-een" in diff, f"de diff miste de oude inhoud: {diff}"
    assert "+drie" in diff, f"de diff miste de nieuwe inhoud: {diff}"
    await connector.close()


@pytest.mark.asyncio
async def test_twee_taken_krijgen_elk_hun_eigen_werkboom(remote: Path) -> None:
    eerste, tweede = _connector(remote), _connector(remote)
    await asyncio.gather(eerste.ensure_repo_cloned(), tweede.ensure_repo_cloned())

    eerste_map, tweede_map = await eerste.get_working_dir(), await tweede.get_working_dir()
    assert eerste_map != tweede_map

    # Een wijziging in de ene werkboom is in de andere niet te zien: dat is de eis uit
    # features/concurrent-write-safety-lost-update.md.
    (Path(eerste_map) / "waarde.txt").write_text("alleen hier")
    assert (Path(tweede_map) / "waarde.txt").read_text() == "drie"
    await eerste.close()
    await tweede.close()


@pytest.mark.asyncio
async def test_beide_taken_houden_hun_commit(remote: Path) -> None:
    """Twee taken op dezelfde repo pushen na elkaar en geen van de twee commits verdwijnt."""
    eerste, tweede = _connector(remote), _connector(remote)
    await eerste.ensure_repo_cloned()
    await tweede.ensure_repo_cloned()

    (Path(await eerste.get_working_dir()) / "van-taak-een.txt").write_text("1")
    (Path(await tweede.get_working_dir()) / "van-taak-twee.txt").write_text("2")
    await asyncio.gather(
        eerste.commit_and_push("van taak een"),
        tweede.commit_and_push("van taak twee"),
    )

    bestanden = _git(remote, "ls-tree", "--name-only", "main")
    assert "van-taak-een.txt" in bestanden
    assert "van-taak-twee.txt" in bestanden
    await eerste.close()
    await tweede.close()


@pytest.mark.asyncio
async def test_een_kopie_met_een_achtergebleven_lock_herstelt_zich(remote: Path) -> None:
    """Een pod die midden in een fetch omvalt laat een index.lock achter."""
    opwarmen = _connector(remote)
    await opwarmen.ensure_repo_cloned()
    await opwarmen.close()

    cache = Path(_warm_cache_path(REPO_URL, "main"))
    (cache / ".git" / "index.lock").write_text("")

    connector = _connector(remote)
    await connector.ensure_repo_cloned()
    werkmap = Path(await connector.get_working_dir())
    assert (werkmap / "waarde.txt").read_text() == "drie"
    assert not (cache / ".git" / "index.lock").exists()
    await connector.close()


@pytest.mark.asyncio
async def test_de_kopie_wordt_bijgewerkt_met_fetch_en_niet_opnieuw_gekloond(remote: Path) -> None:
    eerste = _connector(remote)
    await eerste.ensure_repo_cloned()
    cache = Path(_warm_cache_path(REPO_URL, "main"))
    gemaakt = cache.stat().st_mtime_ns
    await eerste.close()

    _nieuwe_commit_op_de_remote(remote, "later.txt", "later")

    tweede = _connector(remote)
    await tweede.ensure_repo_cloned()
    assert (Path(await tweede.get_working_dir()) / "later.txt").exists()
    assert cache.stat().st_mtime_ns >= gemaakt, "de kopie is weggegooid in plaats van bijgewerkt"
    assert (cache / "later.txt").exists(), "de kopie moet op de nieuwe tip staan"
    await tweede.close()


@pytest.mark.asyncio
async def test_een_lege_repo_valt_terug_op_de_gewone_kloonweg(tmp_path: Path) -> None:
    """Een repo zonder commits kan geen warme kopie hebben; de taak moet toch doorlopen."""
    leeg = tmp_path / "leeg.git"
    leeg.mkdir()
    _git(leeg, "init", "-q", "--bare", "-b", "main", ".")

    connector = _connector(leeg)
    await connector.ensure_repo_cloned()
    assert os.path.isdir(os.path.join(await connector.get_working_dir(), ".git"))
    await connector.close()


@pytest.mark.asyncio
async def test_een_eigen_werkmap_gebruikt_de_warme_kopie_niet(remote: Path, tmp_path: Path) -> None:
    """De ProjectStore brengt zijn eigen warme map mee en is zelf de kopie."""
    eigen = tmp_path / "eigen"
    eigen.mkdir()
    connector = _connector(remote, working_dir=str(eigen))
    await connector.ensure_repo_cloned()

    assert (eigen / "waarde.txt").read_text() == "drie"
    assert not os.path.exists(_warm_cache_path(REPO_URL, "main"))


@pytest.mark.asyncio
async def test_zonder_wijziging_gaat_er_niet_gepusht(remote: Path) -> None:
    connector = _connector(remote)
    await connector.ensure_repo_cloned()

    gepusht: list[list[str]] = []
    echte = connector._run_git_command

    async def tel(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[:1] == ["push"]:
            gepusht.append(list(args))
        return await echte(args, **kwargs)

    connector._run_git_command = tel  # type: ignore[method-assign]
    await connector.commit_and_push("niets veranderd")

    assert gepusht == [], "een commit die niets vastlegde mag geen push kosten"
    await connector.close()


@pytest.mark.asyncio
async def test_de_default_branch_wordt_een_keer_opgevraagd(remote: Path) -> None:
    """``ls-remote --symref`` ging voor elke kloon opnieuw over het netwerk."""
    connector = _connector(remote)
    opvragingen: list[list[str]] = []
    echte = connector._run_git_command

    async def tel(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[:1] == ["ls-remote"]:
            opvragingen.append(list(args))
        return await echte(args, **kwargs)

    connector._run_git_command = tel  # type: ignore[method-assign]
    assert await connector._get_remote_default_branch() == "main"
    assert await connector._get_remote_default_branch() == "main"
    assert len(opvragingen) == 1


@pytest.mark.asyncio
async def test_het_aantal_warme_kopieen_is_begrensd(remote: Path, tmp_path: Path, monkeypatch) -> None:
    """TEMP_DIR is een emptyDir, en een project kiest zijn eigen deployments-repo.

    Zonder grens groeit dit met het aantal verschillende remotes en loopt /tmp vol, en dan
    staat OPI helemaal stil.
    """
    from opi.connectors import git as git_module

    monkeypatch.setattr(git_module, "WARM_CACHE_MAX", 3)

    for n in range(5):
        connector = _connector(remote, url=f"https://example.invalid/repo-{n}.git")
        await connector.ensure_repo_cloned()
        await connector.close()

    kopieen = list(Path(git_module.WARM_CACHE_DIR).iterdir())
    assert len(kopieen) <= 3, f"er staan {len(kopieen)} warme kopieen, de grens is 3"
    # De laatst gebruikte repo staat er nog: er wordt van onder opgeruimd.
    assert Path(_warm_cache_path("https://example.invalid/repo-4.git", "main")).is_dir()


@pytest.mark.asyncio
async def test_een_map_die_sinds_een_revisie_niet_wijzigde_is_bewijsbaar(remote: Path) -> None:
    """De grendel op het projectniveau leunt hierop: is DEZE map al toegepast?

    Op een repo die meerdere projecten delen loopt de tip altijd voor op de laatste
    vergelijking van een applicatie, dus "is onze commit een voorouder" zegt niets. De
    vraag is of precies deze bestanden bij die revisie al zo in de boom stonden.
    """
    connector = _connector(remote)
    await connector.ensure_repo_cloned()
    werkmap = Path(await connector.get_working_dir())
    oud = _git(werkmap, "rev-parse", "HEAD~2")

    # waarde.txt wijzigde wel tussen HEAD~2 en HEAD, een.txt niet.
    assert await connector.path_unchanged_since(oud, "een.txt") is True
    assert await connector.path_unchanged_since(oud, "waarde.txt") is False
    # Een revisie die we niet hebben, en een leeg pad, bewijzen niets.
    assert await connector.path_unchanged_since("0" * 40, "een.txt") is False
    assert await connector.path_unchanged_since(oud, "") is False
    await connector.close()


@pytest.mark.asyncio
async def test_zonder_toegang_komt_er_geen_werkmap_uit_een_kopie_van_een_ander(remote: Path, tmp_path: Path) -> None:
    """De kopie is van de repo-URL zonder credentials en dus gedeeld.

    Elke taak resolvet eerst de branch met zijn EIGEN toegang, zoals de ArgoCD repo-server
    doet. Faalt dat, dan mag de inhoud die een ander project ophaalde er niet uit komen.
    """
    vuller = _connector(remote)
    await vuller.ensure_repo_cloned()
    await vuller.close()
    assert Path(_warm_cache_path(REPO_URL, "main"), ".git").is_dir()

    zonder_toegang = _connector(tmp_path / "bestaat-niet.git")
    werkmap = Path(zonder_toegang._GitConnector__working_dir)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        await zonder_toegang.ensure_repo_cloned()

    assert not (werkmap / "waarde.txt").exists(), "de werkmap kreeg inhoud uit de kopie van een ander project"
    assert not (werkmap / ".git").exists()


@pytest.mark.asyncio
async def test_de_warme_kopie_bewaart_geen_credentials(remote: Path) -> None:
    """Authenticatie gaat per commando via de omgeving, dus niet naar .git/config."""
    connector = GitConnector(
        repo_url=REPO_URL, branch="main", name="deployments", username="opi", password="plain:geheim-token"
    )
    connector._clean_repo_url = str(remote)
    await connector.ensure_repo_cloned()

    config = Path(_warm_cache_path(REPO_URL, "main"), ".git", "config").read_text()
    assert "geheim-token" not in config
    assert "opi@" not in config
    assert "extraHeader" not in config
    await connector.close()


@pytest.mark.asyncio
async def test_een_tweede_taak_op_dezelfde_revisie_doet_geen_fetch(remote: Path) -> None:
    eerste = _connector(remote)
    await eerste.ensure_repo_cloned()
    await eerste.close()

    tweede = _connector(remote)
    gefetcht: list[list[str]] = []
    echte = tweede._run_git_command

    async def tel(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[:1] == ["fetch"]:
            gefetcht.append(list(args))
        return await echte(args, **kwargs)

    tweede._run_git_command = tel  # type: ignore[method-assign]
    await tweede.ensure_repo_cloned()

    assert gefetcht == [], "de revisie stond al in de kopie, dus er hoort geen fetch te zijn"
    assert (Path(await tweede.get_working_dir()) / "waarde.txt").read_text() == "drie"
    await tweede.close()
