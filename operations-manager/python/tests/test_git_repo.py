"""Tests voor GitRepo: de beheerde bare kopie per remote.

Tegen echte git-repo's op schijf, want wat hier mis kan gaan is git-gedrag: dat een write
alleen zijn scope raakt, dat modi en symlinks overleven, dat twee gelijktijdige writers
beide landen, en dat een halve kopie zich herstelt.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import (
    EXEC_MODE,
    SYMLINK_MODE,
    FileChange,
    GitCommandTimeoutError,
    GitPushConflictError,
    GitPushRejectedError,
    GitRemoteUnavailableError,
    push_lock_for,
    run_git,
)
from opi.connectors.git_repo import (
    GitCredentials,
    GitRepo,
    GitRepoConflictError,
    RepoAt,
    get_git_repo,
    reset_git_repos,
    warm_platform_repos,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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


@pytest.fixture(autouse=True)
def _eigen_kopiemap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Elke test zijn eigen map met kopieen, zodat ze elkaar niet zien."""
    import opi.connectors.git_repo as module

    monkeypatch.setattr(module, "GIT_REPOS_DIR", str(tmp_path / "git-repos"))
    reset_git_repos()
    yield
    reset_git_repos()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Een bare remote met twee projectmappen, zoals de deployments-repo ze heeft."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    for project in ("alfa", "beta"):
        folder = seed / "prd" / project
        folder.mkdir(parents=True)
        (folder / "kustomization.yaml").write_text(f"name: {project}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "eerste")

    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    _git(bare, "config", "uploadpack.allowFilter", "true")
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    return bare


def _repo(remote: Path) -> GitRepo:
    return get_git_repo(str(remote), "main")


ANON = GitCredentials()


def _paths_in(bare: Path, ref: str = "main") -> list[str]:
    return sorted(_git(bare, "ls-tree", "-r", "--name-only", ref).splitlines())


async def _write(repo: GitRepo, scope: list[str], source: Path, message: str, base: str | None = None):
    base_ref = base or await repo.tip(ANON)
    return await repo.write_dir(
        base_ref=base_ref,
        scope_paths=scope,
        source_dir=str(source),
        message=message,
        actor=None,
        credentials=ANON,
    )


@pytest.mark.asyncio
async def test_materialize_zet_alleen_de_scope_neer(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    doel = tmp_path / "werk"
    paden = await repo.materialize(["prd/alfa"], await repo.tip(ANON), str(doel), ANON)

    assert paden == ["prd/alfa"]
    assert (doel / "prd" / "alfa" / "kustomization.yaml").read_text() == "name: alfa\n"
    assert not (doel / "prd" / "beta").exists()


@pytest.mark.asyncio
async def test_een_write_raakt_alleen_zijn_scope(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "nieuw.yaml").write_text("x\n")

    resultaat = await _write(repo, ["prd/alfa"], werk, "alfa bijwerken", base)

    assert resultaat.committed
    assert resultaat.paths == ["prd/alfa/nieuw.yaml"]
    assert _paths_in(remote) == [
        "prd/alfa/kustomization.yaml",
        "prd/alfa/nieuw.yaml",
        "prd/beta/kustomization.yaml",
    ]


@pytest.mark.asyncio
async def test_een_verwijderd_bestand_verdwijnt_uit_de_scope(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "kustomization.yaml").unlink()

    await _write(repo, ["prd/alfa"], werk, "alfa leeghalen", base)

    assert _paths_in(remote) == ["prd/beta/kustomization.yaml"]


@pytest.mark.asyncio
async def test_symlink_uitvoerbaar_en_binair_komen_er_ongeschonden_door(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    folder = werk / "prd" / "alfa"
    (folder / "script.sh").write_text("#!/bin/sh\n")
    os.chmod(folder / "script.sh", 0o755)  # noqa: S103 - de modus IS wat deze test meet
    (folder / "plaatje.bin").write_bytes(bytes(range(256)))
    os.symlink("kustomization.yaml", folder / "link.yaml")

    await _write(repo, ["prd/alfa"], werk, "bijzondere bestanden", base)

    modi = {
        regel.split("\t")[1]: regel.split(" ")[0]
        for regel in _git(remote, "ls-tree", "-r", "main", "prd/alfa").splitlines()
    }
    assert modi["prd/alfa/script.sh"] == EXEC_MODE
    assert modi["prd/alfa/link.yaml"] == SYMLINK_MODE

    terug = tmp_path / "terug"
    await repo.materialize(["prd/alfa"], await repo.tip(ANON), str(terug), ANON)
    assert (terug / "prd" / "alfa" / "plaatje.bin").read_bytes() == bytes(range(256))
    assert os.readlink(terug / "prd" / "alfa" / "link.yaml") == "kustomization.yaml"
    assert os.access(terug / "prd" / "alfa" / "script.sh", os.X_OK)


@pytest.mark.asyncio
async def test_twee_projecten_landen_beide(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    base = await repo.tip(ANON)

    async def ronde(project: str) -> None:
        werk = tmp_path / f"werk-{project}"
        await repo.materialize([f"prd/{project}"], base, str(werk), ANON)
        (werk / "prd" / project / f"{project}.yaml").write_text("x\n")
        await _write(repo, [f"prd/{project}"], werk, f"{project} bijwerken", base)

    await asyncio.gather(ronde("alfa"), ronde("beta"))

    assert _paths_in(remote) == [
        "prd/alfa/alfa.yaml",
        "prd/alfa/kustomization.yaml",
        "prd/beta/beta.yaml",
        "prd/beta/kustomization.yaml",
    ]


@pytest.mark.asyncio
async def test_een_echt_conflict_op_hetzelfde_pad_geeft_de_conflictfout(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "kustomization.yaml").write_text("onze versie\n")

    # Iemand anders wijzigt hetzelfde bestand en pusht eerst.
    extern = tmp_path / "extern"
    _git(tmp_path, "clone", "-q", str(remote), str(extern))
    (extern / "prd" / "alfa" / "kustomization.yaml").write_text("hun versie\n")
    _git(extern, "commit", "-aqm", "hun wijziging")
    _git(extern, "push", "-q", "origin", "main")

    with pytest.raises(GitRepoConflictError):
        await _write(repo, ["prd/alfa"], werk, "onze wijziging", base)

    assert _git(remote, "show", "main:prd/alfa/kustomization.yaml") == "hun versie"


@pytest.mark.asyncio
async def test_dezelfde_inhoud_is_geen_conflict(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "nieuw.yaml").write_text("zelfde\n")

    extern = tmp_path / "extern"
    _git(tmp_path, "clone", "-q", str(remote), str(extern))
    (extern / "prd" / "alfa" / "nieuw.yaml").write_text("zelfde\n")
    _git(extern, "add", "-A")
    _git(extern, "commit", "-qm", "zelfde bestand")
    _git(extern, "push", "-q", "origin", "main")

    resultaat = await _write(repo, ["prd/alfa"], werk, "zelfde inhoud", base)

    assert not resultaat.committed


def _blobless_repo(remote: Path) -> GitRepo:
    """Dezelfde remote, maar over ``file://``.

    Git negeert ``--filter`` op een kloon van een lokaal PAD ("ignored in local clones;
    use file:// instead"), en dan zijn alle blobs aanwezig en wordt de promisor-tak nooit
    gelopen. Over ``file://`` is de kopie echt blobless, zoals op productie.
    """
    return get_git_repo(f"file://{remote}", "main")


def _blobs_ontbreken(repo: GitRepo, remote: Path, path: str) -> bool:
    """Of de blob van ``path`` op de tip van de remote echt niet in de kopie staat.

    Met ``GIT_NO_LAZY_FETCH``, want anders haalt ``cat-file -e`` een ontbrekende blob
    stilletjes bij de promisor-remote op en zegt daarna dat hij er is.
    """
    sha = _git(remote, "rev-parse", f"main:{path}")
    done = subprocess.run(
        [GIT, "cat-file", "-e", sha],
        cwd=repo.path,
        env={**_GIT_ENV, "GIT_NO_LAZY_FETCH": "1"},
        capture_output=True,
    )
    return done.returncode != 0


def _hernoem_extern(remote: Path, tmp_path: Path, oud: str, nieuw: str, inhoud: str) -> None:
    """Hernoem een bestand MET inhoudswijziging, zoals een andere taak dat zou pushen."""
    extern = tmp_path / "hernoem"
    _git(tmp_path, "clone", "-q", str(remote), str(extern))
    (extern / oud).unlink()
    (extern / nieuw).write_text(inhoud)
    _git(extern, "add", "-A")
    _git(extern, "commit", "-qm", "hernoemd")
    _git(extern, "push", "-q", "origin", "main")


@pytest.mark.asyncio
async def test_het_oude_pad_van_een_hernoeming_blijft_een_gewijzigd_pad(remote: Path, tmp_path: Path) -> None:
    """Met rename-detectie verdwijnt het oude pad, en mist de conflictgrendel het."""
    repo = _repo(remote)
    base = await repo.tip(ANON)
    _hernoem_extern(remote, tmp_path, "prd/alfa/kustomization.yaml", "prd/alfa/nieuw.yaml", "name: alfa\nextra: 1\n")
    tip = await repo.sync(ANON)

    gewijzigd = await repo.changed_paths(base, tip, "prd/alfa")

    assert sorted(gewijzigd) == ["prd/alfa/kustomization.yaml", "prd/alfa/nieuw.yaml"]


@pytest.mark.asyncio
async def test_een_conflict_wordt_ook_gezien_op_een_echt_blobless_kopie(remote: Path, tmp_path: Path) -> None:
    repo = _blobless_repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    assert _blobs_ontbreken(repo, remote, "prd/beta/kustomization.yaml")
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "kustomization.yaml").write_text("onze versie\n")

    _hernoem_extern(remote, tmp_path, "prd/alfa/kustomization.yaml", "prd/alfa/nieuw.yaml", "name: alfa\nextra: 1\n")
    await repo.sync(ANON)
    # De blob van het hernoemde bestand is er niet: rename-detectie zou hem bij de
    # promisor-remote moeten ophalen, zonder credentials.
    assert _blobs_ontbreken(repo, remote, "prd/alfa/nieuw.yaml")

    with pytest.raises(GitRepoConflictError):
        await _write(repo, ["prd/alfa"], werk, "onze wijziging", base)

    assert _paths_in(remote) == ["prd/alfa/nieuw.yaml", "prd/beta/kustomization.yaml"]


@pytest.mark.asyncio
async def test_gewijzigde_paden_vragen_geen_blob_bij_de_promisor_remote(remote: Path, tmp_path: Path) -> None:
    """De conflictgrendel moet werken zonder netwerk: met rename-detectie deed hij dat niet.

    Inexacte rename-detectie heeft de blob-inhoud nodig, en op een blobless kopie wordt
    die bij de promisor-remote opgehaald. Die fetch krijgt geen credentials mee en faalt
    tegen een remote die inloggen eist.
    """
    repo = _blobless_repo(remote)
    base = await repo.tip(ANON)
    _hernoem_extern(remote, tmp_path, "prd/alfa/kustomization.yaml", "prd/alfa/nieuw.yaml", "name: alfa\nextra: 1\n")
    tip = await repo.sync(ANON)
    _git(Path(repo.path), "remote", "set-url", "origin", f"file://{tmp_path}/weg.git")

    gewijzigd = await repo.changed_paths(base, tip, "prd/alfa")

    assert sorted(gewijzigd) == ["prd/alfa/kustomization.yaml", "prd/alfa/nieuw.yaml"]


NIET_ASCII = "prd/alfa/caf\u00e9-chart.yaml"


def _seed_niet_ascii(remote: Path, tmp_path: Path) -> None:
    """Zet een bestand met niet-ASCII bytes in de scope, zoals een helm-chart van een gebruiker."""
    extern = tmp_path / "seed-niet-ascii"
    _git(tmp_path, "clone", "-q", str(remote), str(extern))
    (extern / NIET_ASCII).write_text("naam: caf\u00e9\n")
    _git(extern, "add", "-A")
    _git(extern, "commit", "-qm", "chart met accent")
    _git(extern, "push", "-q", "origin", "main")


@pytest.mark.asyncio
async def test_een_bestandsnaam_met_accent_overleeft_een_write(remote: Path, tmp_path: Path) -> None:
    _seed_niet_ascii(remote, tmp_path)
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "nieuw.yaml").write_text("x\n")

    resultaat = await _write(repo, ["prd/alfa"], werk, "alfa bijwerken", base)

    assert resultaat.paths == ["prd/alfa/nieuw.yaml"]
    assert _git(remote, "ls-tree", "-r", "--name-only", "-z", "main").split("\0").count(NIET_ASCII) == 1


@pytest.mark.asyncio
async def test_een_conflict_op_een_naam_met_accent_wordt_gezien(remote: Path, tmp_path: Path) -> None:
    _seed_niet_ascii(remote, tmp_path)
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / NIET_ASCII).write_text("onze versie\n")

    extern = tmp_path / "extern"
    _git(tmp_path, "clone", "-q", str(remote), str(extern))
    (extern / NIET_ASCII).write_text("hun versie\n")
    _git(extern, "commit", "-aqm", "hun wijziging")
    _git(extern, "push", "-q", "origin", "main")

    with pytest.raises(GitRepoConflictError):
        await _write(repo, ["prd/alfa"], werk, "onze wijziging", base)


@pytest.mark.asyncio
async def test_een_ronde_zonder_wijziging_commit_en_pusht_niet(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)

    resultaat = await _write(repo, ["prd/alfa"], werk, "niets veranderd", base)

    assert not resultaat.committed
    assert resultaat.ref == base
    assert _git(remote, "rev-parse", "main") == base


@pytest.mark.asyncio
async def test_de_kopie_bevat_geen_credential(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "x.txt").write_text("x")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "eerste")

    repo = GitRepo("https://gebruiker:geheim@localhost/pad.git")
    assert "geheim" not in repo.url

    # Een lokale remote, want de kopie moet echt op schijf staan om hem te kunnen lezen.
    lokaal = get_git_repo(str(seed), "main")
    await lokaal.tip(GitCredentials(username="gebruiker", password="geheim"))
    inhoud = "\n".join(
        (Path(root) / name).read_text(errors="ignore")
        for root, _, files in os.walk(lokaal.path)
        for name in files
        if name in ("config", "FETCH_HEAD")
    )
    assert "geheim" not in inhoud


@pytest.mark.asyncio
async def test_een_tweede_materialize_op_dezelfde_sha_doet_geen_fetch(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    sha = await repo.tip(ANON)
    fetches: list[str] = []

    import opi.connectors.git_repo as module

    echte_fetch = module.GitRepo._fetch

    async def tel_fetch(self, credentials):  # type: ignore[no-untyped-def]
        fetches.append(self.path)
        return await echte_fetch(self, credentials)

    module.GitRepo._fetch = tel_fetch  # type: ignore[method-assign]
    try:
        await repo.tip(ANON)
        await repo.materialize(["prd/alfa"], sha, str(tmp_path / "tweede"), ANON)
    finally:
        module.GitRepo._fetch = echte_fetch  # type: ignore[method-assign]

    assert fetches == []


@pytest.mark.asyncio
async def test_de_kopie_herstelt_zich_na_een_achtergebleven_index_lock(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    await repo.tip(ANON)

    # Een kopie die geen git-repo meer is: alles weg behalve een achtergebleven lock.
    shutil.rmtree(repo.path)
    os.makedirs(repo.path)
    (Path(repo.path) / "index.lock").write_text("")

    sha = await repo.tip(ANON)

    assert sha == _git(remote, "rev-parse", "main")
    assert await repo.read_file("prd/alfa/kustomization.yaml", sha) == b"name: alfa\n"


@pytest.mark.asyncio
async def test_path_unchanged_since_vergelijkt_byte_voor_byte(remote: Path, tmp_path: Path) -> None:
    """Bewijst dat een pad tussen twee revisies ongewijzigd is, voor de project-gate van
    RC-418 (``_project_level_already_applied``). Een onbekende revisie en een leeg pad
    bewijzen niets, dus False."""
    repo = _repo(remote)
    oud = await repo.tip(ANON)
    werk = tmp_path / "werk"
    await repo.materialize(["prd/alfa"], oud, str(werk), ANON)
    (werk / "prd" / "alfa" / "nieuw.yaml").write_text("x\n")
    nieuw = (await _write(repo, ["prd/alfa"], werk, "nieuw bestand", oud)).ref

    assert await repo.path_unchanged_since(oud, "prd/alfa/kustomization.yaml", nieuw) is True
    assert await repo.path_unchanged_since(oud, "prd/alfa/nieuw.yaml", nieuw) is False
    assert await repo.path_unchanged_since("0" * 40, "prd/alfa/kustomization.yaml", nieuw) is False
    assert await repo.path_unchanged_since(oud, "", nieuw) is False


@pytest.mark.asyncio
async def test_de_werkmap_path_unchanged_since_gebruikt_zijn_eigen_laatste_stand(remote: Path) -> None:
    """``GitRepoWorkspace.path_unchanged_since`` vergelijkt tegen de stand waar de werkmap
    NU op staat: voor een write is dat de base, na een write de commit die net landde."""
    werkmap = _workspace(remote, ["prd/alfa"])
    try:
        # Nog geen werkmap, dus geen stand om tegen te vergelijken.
        assert await werkmap.path_unchanged_since("iets", "prd/alfa/kustomization.yaml") is False

        oud = await werkmap.refresh()

        async def _genereer() -> None:
            Path(await werkmap.get_working_dir(), "prd", "alfa", "nieuw.yaml").write_text("x\n")

        await werkmap.mutate_and_push("alfa uitbreiden", _genereer)

        assert await werkmap.path_unchanged_since(oud, "prd/alfa/kustomization.yaml") is True
        assert await werkmap.path_unchanged_since(oud, "prd/alfa/nieuw.yaml") is False
    finally:
        await werkmap.close()


@pytest.mark.asyncio
async def test_een_pad_buiten_de_scope_wordt_geweigerd(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa", "prd/beta"], base, str(werk), ANON)
    (werk / "prd" / "beta" / "ingeslopen.yaml").write_text("x\n")

    await _write(repo, ["prd/alfa"], werk, "alleen alfa", base)

    assert "prd/beta/ingeslopen.yaml" not in _paths_in(remote)


@pytest.mark.asyncio
async def test_een_plaintext_secret_wordt_geweigerd(remote: Path, tmp_path: Path) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "geheim.to-sops.yaml").write_text("wachtwoord: 1234\n")

    with pytest.raises(RuntimeError, match="to-sops"):
        await _write(repo, ["prd/alfa"], werk, "secret", base)


@pytest.mark.asyncio
async def test_commit_leest_opnieuw_op_de_nieuwe_tip_na_een_afgewezen_push(remote: Path, tmp_path: Path) -> None:
    """De mutate-lus van de ProjectStore, bovenop GitRepo.commit.

    ``build_changes`` leest een YAML-bestand, past het aan en valideert het. Tussen de
    eerste poging en de push landt een externe write; de tweede poging moet die stand
    lezen en zijn wijziging daarop toepassen.
    """
    repo = _repo(remote)
    gelezen: list[bytes] = []
    extern_geland = False

    async def build_changes(at: RepoAt) -> dict[str, FileChange | None]:
        nonlocal extern_geland
        huidig = await at.read_file("prd/alfa/kustomization.yaml") or b""
        gelezen.append(huidig)
        if not extern_geland:
            # Een gelijktijdige externe write, precies tussen lezen en pushen.
            extern = tmp_path / "extern"
            _git(tmp_path, "clone", "-q", str(remote), str(extern))
            (extern / "prd" / "alfa" / "kustomization.yaml").write_bytes(huidig + b"extern: true\n")
            _git(extern, "commit", "-aqm", "extern")
            _git(extern, "push", "-q", "origin", "main")
            extern_geland = True
        return {"prd/alfa/kustomization.yaml": FileChange(huidig + b"onze: true\n")}

    resultaat = await repo.commit(build_changes, message="onze regel erbij", actor=None, credentials=ANON)

    assert resultaat.committed
    assert gelezen == [b"name: alfa\n", b"name: alfa\nextern: true\n"]
    assert _git(remote, "show", "main:prd/alfa/kustomization.yaml") == "name: alfa\nextern: true\nonze: true"


@pytest.mark.asyncio
async def test_push_geeft_het_op_na_de_grens(remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(remote)

    async def build_changes(at: RepoAt) -> dict[str, FileChange | None]:
        # Elke poging botst: iemand anders pusht steeds eerst.
        extern = tmp_path / f"extern-{len(os.listdir(tmp_path))}"
        _git(tmp_path, "clone", "-q", str(remote), str(extern))
        (extern / "teller.txt").write_text(str(len(os.listdir(tmp_path))))
        _git(extern, "add", "-A")
        _git(extern, "commit", "-qm", "extern")
        _git(extern, "push", "-q", "origin", "main")
        return {"prd/alfa/onze.yaml": FileChange(b"x\n")}

    with pytest.raises(GitPushConflictError):
        await repo.commit(build_changes, message="botst altijd", actor=None, credentials=ANON)


# ---------------------------------------------------------------------------
# De werkmap van een taak
# ---------------------------------------------------------------------------


def _workspace(remote: Path, scope: list[str]):
    from opi.connectors.git_repo import GitRepoWorkspace

    return GitRepoWorkspace(_repo(remote), ANON, scope, project_name="alfa")


@pytest.mark.asyncio
async def test_de_werkmap_schrijft_de_stand_van_zijn_mutatie_weg(remote: Path) -> None:
    werkmap = _workspace(remote, ["prd/alfa"])
    try:

        async def _genereer() -> str:
            Path(await werkmap.get_working_dir(), "prd", "alfa", "deployment.yaml").write_text("kind: Deployment\n")
            return "klaar"

        assert await werkmap.mutate_and_push("alfa genereren", _genereer) == "klaar"
        assert werkmap.last_write is not None
        assert werkmap.last_write.paths == ["prd/alfa/deployment.yaml"]
        assert "prd/alfa/deployment.yaml" in _paths_in(remote)
    finally:
        await werkmap.close()


@pytest.mark.asyncio
async def test_een_hernoeming_van_een_eerdere_stap_komt_in_de_volgende_write_mee(remote: Path) -> None:
    """De PVC-hernoeming van ``handle_service_removal`` commit niet zelf.

    Die hernoemt een bestand in de werkmap en vertrouwt erop dat de generatie erna hem
    meeneemt. Vult de volgende stap de werkmap opnieuw, dan is die hernoeming weg.
    """
    werkmap = _workspace(remote, ["prd/alfa"])
    try:
        folder = Path(await werkmap.get_working_dir(), "prd", "alfa")
        (folder / "web-data-pvc.yaml").write_text("kind: PersistentVolumeClaim\n")
        (folder / "web-data-pvc.yaml").rename(folder / "web-data-pvc.marked-for-deletion.yaml")

        async def _genereer() -> None:
            Path(await werkmap.get_working_dir(), "prd", "alfa", "deployment.yaml").write_text("kind: Deployment\n")

        await werkmap.mutate_and_push("alfa opnieuw genereren", _genereer)

        assert "prd/alfa/web-data-pvc.marked-for-deletion.yaml" in _paths_in(remote)
    finally:
        await werkmap.close()


@pytest.mark.asyncio
async def test_een_tweede_write_bouwt_op_de_commit_van_de_eerste(remote: Path) -> None:
    """De infrastructuurstap en de manifeststap zijn twee writes op dezelfde werkmap."""
    werkmap = _workspace(remote, ["prd/alfa"])
    try:

        async def _infra() -> None:
            folder = Path(await werkmap.get_working_dir(), "prd", "alfa", "infrastructure")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "db-cluster.yaml").write_text("kind: Cluster\n")

        async def _manifests() -> None:
            folder = Path(await werkmap.get_working_dir(), "prd", "alfa", "productie")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "deployment.yaml").write_text("kind: Deployment\n")

        await werkmap.mutate_and_push("infra", _infra)
        await werkmap.mutate_and_push("manifests", _manifests)

        assert _paths_in(remote) == [
            "prd/alfa/infrastructure/db-cluster.yaml",
            "prd/alfa/kustomization.yaml",
            "prd/alfa/productie/deployment.yaml",
            "prd/beta/kustomization.yaml",
        ]
    finally:
        await werkmap.close()


@pytest.mark.asyncio
async def test_de_werkmap_laat_niets_achter_na_close(remote: Path) -> None:
    werkmap = _workspace(remote, ["prd/alfa"])
    pad = await werkmap.get_working_dir()
    await werkmap.close()

    assert not os.path.exists(pad)


@pytest.mark.asyncio
async def test_remove_and_push_buiten_de_scope_wordt_geweigerd(remote: Path) -> None:
    werkmap = _workspace(remote, ["prd/alfa"])
    try:
        with pytest.raises(ValueError, match="buiten de scope"):
            await werkmap.remove_and_push("prd/beta", "beta weghalen")
    finally:
        await werkmap.close()


@pytest.mark.asyncio
async def test_remove_and_push_meldt_een_pad_dat_er_niet_stond(remote: Path) -> None:
    werkmap = _workspace(remote, ["prd/alfa"])
    try:
        assert await werkmap.remove_and_push("prd/alfa/productie", "weg") is False
        assert await werkmap.remove_and_push("prd/alfa", "weg") is True
        assert _paths_in(remote) == ["prd/beta/kustomization.yaml"]
    finally:
        await werkmap.close()


@pytest.mark.asyncio
async def test_een_kopie_die_in_gebruik_is_overleeft_de_opruiming(remote: Path, tmp_path: Path) -> None:
    """Een prune mag de kopie niet weghalen onder een lopende git archive of commit."""
    import opi.connectors.git_repo as module

    repo = _repo(remote)
    base = await repo.tip(ANON)
    assert os.path.isdir(repo.path)

    begonnen = asyncio.Event()
    vrij = asyncio.Event()

    async def trage_lezer() -> None:
        async with repo._in_use():
            begonnen.set()
            await vrij.wait()

    taak = asyncio.create_task(trage_lezer())
    await begonnen.wait()
    grens = module.GIT_REPOS_MAX_BYTES
    try:
        module.GIT_REPOS_MAX_BYTES = 0
        module._prune_repos(keep="/bestaat-niet")
    finally:
        module.GIT_REPOS_MAX_BYTES = grens
        vrij.set()
        await taak

    assert os.path.isdir(repo.path)
    assert await repo.materialize(["prd/alfa"], base, str(tmp_path / "werk"), ANON) == ["prd/alfa"]


@pytest.mark.asyncio
async def test_een_pad_met_een_regeleinde_kan_niet_buiten_de_scope_schrijven(remote: Path, tmp_path: Path) -> None:
    """Een bestandsnaam met een nieuwe regel mag geen tweede index-regel worden.

    De stroom naar ``update-index --index-info`` is een regel per pad, dus zo'n naam zou
    een pad buiten de scope kunnen zetten: de scope-controle ziet de sleutel, git de
    regels. Zulke namen komen uit een gekopieerde helm- of helmfile-boom van de gebruiker.
    """
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)

    # Een bestandsnaam kan geen "/" bevatten, dus zit het regeleinde in een MAPnaam en
    # leveren de mappen eronder de rest van het ingespoten pad.
    blob = _git(remote, "rev-parse", "main:prd/alfa/kustomization.yaml")
    ingespoten = werk / "prd" / "alfa" / f"map\n100644 {blob}\tprd" / "beta"
    ingespoten.mkdir(parents=True)
    (ingespoten / "kustomization.yaml").write_bytes(b"onschuldig\n")

    with pytest.raises(ValueError, match="regeleinde"):
        await _write(repo, ["prd/alfa"], werk, "alfa bijwerken", base)

    assert _git(remote, "rev-parse", "main") == base
    assert _git(remote, "cat-file", "blob", "main:prd/beta/kustomization.yaml") == "name: beta"


# ---------------------------------------------------------------------------
# The local tip is the truth: when the network is involved at all
# ---------------------------------------------------------------------------


def _subcommand(args: list[str]) -> str:
    """The git subcommand, past any leading ``-c key=value`` options."""
    index = 0
    while args[index] == "-c":
        index += 2
    return args[index]


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The git subcommands GitRepo runs, in order."""
    import opi.connectors.git_repo as module

    calls: list[str] = []
    real = module.run_git

    async def count(args: list[str], **kwargs: object) -> tuple[str, str, int]:
        calls.append(_subcommand(args))
        return await real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "run_git", count)
    return calls


def _commit_outside(remote: Path, tmp_path: Path, path: str, content: str) -> str:
    """A commit that lands on the remote outside ZAD, like a manual change."""
    outside = tmp_path / f"outside-{path.replace('/', '-')}"
    _git(tmp_path, "clone", "-q", str(remote), str(outside))
    (outside / path).parent.mkdir(parents=True, exist_ok=True)
    (outside / path).write_text(content)
    _git(outside, "add", "-A")
    _git(outside, "commit", "-qm", f"outside: {path}")
    _git(outside, "push", "-q", "origin", "main")
    return _git(remote, "rev-parse", "main")


@pytest.mark.asyncio
async def test_two_writes_in_a_row_touch_no_network(remote: Path, tmp_path: Path, git_calls: list[str]) -> None:
    """The ordinary path: nothing from outside, so no ls-remote and no fetch."""
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    git_calls.clear()

    (werk / "prd" / "alfa" / "eerste.yaml").write_text("x: 1\n")
    assert (await _write(repo, ["prd/alfa"], werk, "eerste")).committed
    (werk / "prd" / "alfa" / "tweede.yaml").write_text("x: 2\n")
    assert (await _write(repo, ["prd/alfa"], werk, "tweede")).committed

    assert "ls-remote" not in git_calls
    assert "fetch" not in git_calls


@pytest.mark.asyncio
async def test_a_commit_from_outside_is_caught_by_the_push(remote: Path, tmp_path: Path, git_calls: list[str]) -> None:
    """Without an ls-remote per write, the rejected push is the catch, with exactly one fetch."""
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    outside = _commit_outside(remote, tmp_path, "prd/beta/extern.yaml", "extern: true\n")
    git_calls.clear()

    (werk / "prd" / "alfa" / "onze.yaml").write_text("onze: true\n")
    result = await _write(repo, ["prd/alfa"], werk, "onze wijziging")

    assert result.committed
    assert git_calls.count("fetch") == 1
    assert "ls-remote" not in git_calls
    assert outside in _git(remote, "rev-list", "main")
    assert _paths_in(remote) == [
        "prd/alfa/kustomization.yaml",
        "prd/alfa/onze.yaml",
        "prd/beta/extern.yaml",
        "prd/beta/kustomization.yaml",
    ]


@pytest.mark.asyncio
async def test_a_concurrent_write_of_our_own_lands_without_a_fetch(remote: Path, git_calls: list[str]) -> None:
    """Two projects build on the same tip: after the other one pushed, its tip is already local."""
    repo = _repo(remote)
    await repo.tip(ANON)
    git_calls.clear()
    other_landed = False

    async def other_project(_at: RepoAt) -> dict[str, FileChange | None]:
        return {"prd/beta/ander.yaml": FileChange(b"ander: true\n")}

    async def build_changes(at: RepoAt) -> dict[str, FileChange | None]:
        nonlocal other_landed
        if not other_landed:
            other_landed = True
            # A write of ANOTHER project, in this process, right between read and push.
            await repo.commit(other_project, message="ander project", actor=None, credentials=ANON)
        return {"prd/alfa/onze.yaml": FileChange(b"onze: true\n")}

    result = await repo.commit(build_changes, message="ons project", actor=None, credentials=ANON)

    assert result.committed
    assert "fetch" not in git_calls
    assert "ls-remote" not in git_calls
    assert _paths_in(remote) == [
        "prd/alfa/kustomization.yaml",
        "prd/alfa/onze.yaml",
        "prd/beta/ander.yaml",
        "prd/beta/kustomization.yaml",
    ]


@pytest.mark.asyncio
async def test_sync_fetches_only_when_the_remote_moved_ahead(
    remote: Path, tmp_path: Path, git_calls: list[str]
) -> None:
    repo = _repo(remote)
    base = await repo.tip(ANON)
    git_calls.clear()

    assert await repo.sync(ANON) == base
    assert git_calls.count("ls-remote") == 1
    assert "fetch" not in git_calls

    outside = _commit_outside(remote, tmp_path, "prd/beta/extern.yaml", "extern: true\n")
    git_calls.clear()

    assert await repo.sync(ANON) == outside
    assert git_calls.count("fetch") == 1
    assert await repo.tip(ANON) == outside


@pytest.mark.asyncio
async def test_sync_raises_when_the_remote_branch_is_gone(remote: Path) -> None:
    repo = _repo(remote)
    await repo.tip(ANON)
    _git(remote, "update-ref", "-d", "refs/heads/main")

    with pytest.raises(RuntimeError, match="has no branch main"):
        await repo.sync(ANON)


@pytest.mark.asyncio
async def test_the_first_tip_clones_and_does_not_fetch(remote: Path, git_calls: list[str]) -> None:
    repo = _repo(remote)

    sha = await repo.tip(ANON)

    assert sha == _git(remote, "rev-parse", "main")
    assert "clone" in git_calls
    assert "fetch" not in git_calls


# ---------------------------------------------------------------------------
# Network timeout
# ---------------------------------------------------------------------------


class _HangingProcess:
    """A git process that never answers, and remembers that it was killed."""

    def __init__(self) -> None:
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        raise AssertionError("onbereikbaar")

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = -9
        return self.returncode


def _hang_on(subcommand: str, monkeypatch: pytest.MonkeyPatch) -> tuple[list[_HangingProcess], Callable[[], int]]:
    """Make only ``git <subcommand>`` hang; everything else is a real git process.

    So the whole chain really runs: cloning, ls-remote, building the commit, and only the
    one network step we want to stall hangs.
    """
    import opi.connectors.git as git_module

    hanging: list[_HangingProcess] = []
    echt = git_module.asyncio.create_subprocess_exec
    aantal = 0

    async def fake_exec(*cmd: str, **kwargs: object):
        nonlocal aantal
        if subcommand in cmd:
            aantal += 1
            process = _HangingProcess()
            hanging.append(process)
            return process
        return await echt(*cmd, **kwargs)

    monkeypatch.setattr(git_module.asyncio, "create_subprocess_exec", fake_exec)
    return hanging, lambda: aantal


@pytest.fixture
def _korte_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import opi.connectors.git_repo as module

    monkeypatch.setattr(module, "GIT_PUSH_TIMEOUT_SECONDS", 0.05)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_korte_timeout")
async def test_een_push_zonder_antwoord_faalt_snel_en_schiet_het_proces_af(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "nieuw.yaml").write_text("x\n")
    hanging, pushes = _hang_on("push", monkeypatch)

    with pytest.raises(GitCommandTimeoutError) as gevangen:
        await _write(repo, ["prd/alfa"], werk, "alfa bijwerken", base)

    melding = str(gevangen.value)
    assert melding.startswith("Push naar ")
    assert f"{remote}#main" in melding
    assert "0.05s" in melding
    assert pushes() == 1, "een push die niet antwoordt wordt niet opnieuw geprobeerd"
    assert hanging[0].killed, "het hangende git-proces moet worden afgeschoten"
    assert not push_lock_for(str(remote), "main").locked(), "de push-lock moet daarna vrij zijn"
    assert _git(remote, "rev-parse", "main") == base


@pytest.mark.asyncio
async def test_na_een_push_time_out_landt_een_volgende_write_wel(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The (repo, branch) lock is free after a timeout, so the next task gets through."""
    import opi.connectors.git_repo as module

    repo = _repo(remote)
    werk = tmp_path / "werk"
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(werk), ANON)
    (werk / "prd" / "alfa" / "nieuw.yaml").write_text("x\n")

    # The short timeout only while the push hangs: the second, real push must not race
    # 50ms, which a slower machine loses.
    with monkeypatch.context() as hangend:
        hangend.setattr(module, "GIT_PUSH_TIMEOUT_SECONDS", 0.05)
        _hang_on("push", hangend)
        with pytest.raises(GitCommandTimeoutError):
            await _write(repo, ["prd/alfa"], werk, "alfa bijwerken", base)

    resultaat = await _write(repo, ["prd/alfa"], werk, "alfa bijwerken")

    assert resultaat.committed
    assert "prd/alfa/nieuw.yaml" in _paths_in(remote)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_korte_timeout")
async def test_een_fetch_zonder_antwoord_bouwt_de_kopie_niet_opnieuw_op(
    remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hanging fetch is not a damaged copy: cloning again would hang as well."""
    repo = _repo(remote)
    await repo.tip(ANON)
    hanging, _ = _hang_on("fetch", monkeypatch)

    with pytest.raises(GitCommandTimeoutError, match=r"^Fetch van "):
        await repo.fetch(ANON)

    assert hanging[0].killed
    assert os.path.isdir(repo.path), "de kopie blijft staan"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_korte_timeout")
async def test_een_ls_remote_zonder_antwoord_faalt_snel(remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The network step of a sync is ls-remote, so it gets the same limit."""
    repo = _repo(remote)
    hanging, _ = _hang_on("ls-remote", monkeypatch)

    with pytest.raises(GitCommandTimeoutError, match=r"^Opvragen van "):
        await repo.sync(ANON)

    assert hanging[0].killed


@pytest.mark.asyncio
async def test_een_commando_zonder_time_out_wacht_gewoon_af(remote: Path) -> None:
    stdout, stderr, code = await run_git(["ls-remote", str(remote), "refs/heads/main"], cwd=str(remote))

    assert code == 0
    assert "refs/heads/main" in stdout
    assert stderr == ""


# ---------------------------------------------------------------------------
# Outages: what is retried, what is not, and what the user reads
# ---------------------------------------------------------------------------

_DNS_FAILURE = "fatal: unable to access 'https://host/repo.git/': Could not resolve host: host"
_REJECTION = (
    " ! [remote rejected] main -> main (pre-receive hook declined)\n"
    "error: failed to push some refs to 'https://host/repo.git'"
)
_REF_COLLISION = (
    " ! [remote rejected] main -> main (cannot lock ref 'refs/heads/main': is at aaa but expected bbb)\n"
    "error: failed to push some refs to 'https://host/repo.git'"
)
_AUTH_FAILURE = "remote: Invalid username or password\nfatal: Authentication failed for 'https://host/repo.git/'"


def _fail_git(
    monkeypatch: pytest.MonkeyPatch,
    subcommand: str,
    stderr: str,
    *,
    times: int | None = None,
) -> list[str]:
    """Make ``git <subcommand>`` fail with this stderr; every other command really runs.

    ``times`` limits it to the first n calls, so recovery after an outage can be measured.
    The returned list is every git subcommand GitRepo ran, in order.
    """
    import opi.connectors.git_repo as module

    real = module.run_git
    calls: list[str] = []

    async def fake(args: list[str], **kwargs: object) -> tuple[str, str, int]:
        calls.append(_subcommand(args))
        if _subcommand(args) == subcommand and (times is None or calls.count(subcommand) <= times):
            return "", stderr, 128
        return await real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "run_git", fake)
    return calls


@pytest.fixture
def _short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry budget that allows exactly one retry, without really waiting."""
    import opi.connectors.git_repo as module

    monkeypatch.setattr(module, "GIT_RETRY_BUDGET_SECONDS", 0.01)


async def _ready_to_write(repo: GitRepo, work: Path) -> str:
    """Materialize the scope and put one new file in it, ready for a write."""
    base = await repo.tip(ANON)
    await repo.materialize(["prd/alfa"], base, str(work), ANON)
    (work / "prd" / "alfa" / "nieuw.yaml").write_text("x\n")
    return base


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_a_brief_outage_does_not_cost_a_task(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first push trips over DNS, the second lands: the commit arrives all the same."""
    repo = _repo(remote)
    work = tmp_path / "work"
    base = await _ready_to_write(repo, work)
    calls = _fail_git(monkeypatch, "push", _DNS_FAILURE, times=1)

    result = await _write(repo, ["prd/alfa"], work, "alfa bijwerken", base)

    assert result.committed
    assert calls.count("push") == 2, "exactly one retry"
    assert "prd/alfa/nieuw.yaml" in _paths_in(remote)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_a_lasting_outage_names_the_platform_and_the_stderr(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(remote)
    work = tmp_path / "work"
    base = await _ready_to_write(repo, work)
    _fail_git(monkeypatch, "push", _DNS_FAILURE)

    with pytest.raises(GitRemoteUnavailableError) as caught:
        await _write(repo, ["prd/alfa"], work, "alfa bijwerken", base)

    message = str(caught.value)
    assert "is niet bereikbaar, ook niet na herhalen" in message
    assert "Dit ligt aan het platform, niet aan het project" in message
    assert "Could not resolve host" in message
    assert _git(remote, "rev-parse", "main") == base


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_an_unreachable_remote_keeps_the_warm_copy(remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebuilding belongs to a broken copy, not to an unreachable remote."""
    repo = _repo(remote)
    await repo.tip(ANON)
    calls = _fail_git(monkeypatch, "fetch", _DNS_FAILURE)

    with pytest.raises(GitRemoteUnavailableError):
        await repo.fetch(ANON)

    assert os.path.isdir(repo.path), "the copy stays in place"
    assert "clone" not in calls, "a clone would run into the same wall"


@pytest.mark.asyncio
async def test_fetch_on_a_broken_copy_rebuilds_it(remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic fetch failure (no network, no auth) keeps throwing the copy away."""
    repo = _repo(remote)
    await repo.tip(ANON)
    calls = _fail_git(monkeypatch, "fetch", "error: unable to read refs: index.lock")

    sha = await repo.fetch(ANON)

    assert sha == _git(remote, "rev-parse", "main")
    assert "clone" in calls, "the broken copy is built again"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_a_rejection_by_the_server_is_not_retried(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "failed to push some refs" alone is no collision: git prints it on every refusal."""
    repo = _repo(remote)
    work = tmp_path / "work"
    base = await _ready_to_write(repo, work)
    calls = _fail_git(monkeypatch, "push", _REJECTION)

    with pytest.raises(GitPushRejectedError) as caught:
        await _write(repo, ["prd/alfa"], work, "alfa bijwerken", base)

    assert "pre-receive hook declined" in str(caught.value)
    assert calls.count("push") == 1
    assert "fetch" not in calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_a_ref_that_cannot_be_locked_is_a_collision(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cannot lock ref`` arrives as ``[remote rejected]`` but means the remote moved."""
    repo = _repo(remote)
    work = tmp_path / "work"
    base = await _ready_to_write(repo, work)
    calls = _fail_git(monkeypatch, "push", _REF_COLLISION)

    with pytest.raises(GitPushConflictError) as caught:
        await _write(repo, ["prd/alfa"], work, "alfa bijwerken", base)

    assert "de remote is verder" in str(caught.value)
    assert calls.count("fetch") >= 1, "a collision is retried on the new tip"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_refused_credentials_are_not_retried(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(remote)
    work = tmp_path / "work"
    base = await _ready_to_write(repo, work)
    calls = _fail_git(monkeypatch, "push", _AUTH_FAILURE)

    with pytest.raises(RuntimeError) as caught:
        await _write(repo, ["prd/alfa"], work, "alfa bijwerken", base)

    assert not isinstance(caught.value, GitRemoteUnavailableError)
    assert "weigert de inloggegevens van OPI" in str(caught.value)
    assert calls.count("push") == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_a_credential_in_the_stderr_stays_out_of_the_message(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the URL shape from the stderr; ``redact_text`` does not know URL userinfo."""
    from opi.utils.logging_redact import redact_text

    repo = _repo(remote)
    work = tmp_path / "work"
    base = await _ready_to_write(repo, work)
    _fail_git(
        monkeypatch,
        "push",
        "fatal: unable to access 'https://git:secret-token@host/repo.git/': Connection refused",
    )

    with pytest.raises(GitRemoteUnavailableError) as caught:
        await _write(repo, ["prd/alfa"], work, "alfa bijwerken", base)

    assert "secret-token" not in redact_text(f"{type(caught.value).__name__}: {caught.value}")


@pytest.mark.asyncio
async def test_an_unclassified_failure_does_not_blame_the_platform(
    remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Network, auth and collisions each have their own wording; anything else may just as
    well be caused by what the project had us write, so it gets no "platform" claim."""
    repo = _repo(remote)
    _fail_git(monkeypatch, "clone", "fatal: repository not found")

    with pytest.raises(RuntimeError) as caught:
        await repo.tip(ANON)

    message = str(caught.value)
    assert "repository not found" in message
    assert "ligt aan het platform" not in message


@pytest.mark.asyncio
async def test_a_clone_without_an_answer_leaves_no_half_copy(remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import opi.connectors.git_repo as module

    monkeypatch.setattr(module, "GIT_CLONE_TIMEOUT_SECONDS", 0.05)
    repo = _repo(remote)
    hanging, clones = _hang_on("clone", monkeypatch)

    with pytest.raises(GitCommandTimeoutError, match=r"^Klonen van "):
        await repo.tip(ANON)

    assert clones() == 1, "a clone that does not answer is not tried again"
    assert hanging[0].killed
    assert not os.path.isdir(repo.path), "the half-finished copy is cleaned up"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_cloning_retries_after_a_brief_outage(remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The clone runs through the same retry as push and fetch, not only the timeout."""
    repo = _repo(remote)
    calls = _fail_git(monkeypatch, "clone", _DNS_FAILURE, times=1)

    await repo.tip(ANON)

    assert calls.count("clone") == 2, "exactly one retry"
    assert os.path.isdir(repo.path)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_cloning_cleans_up_the_half_copy_after_a_lasting_outage(
    remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(remote)
    _fail_git(monkeypatch, "clone", _DNS_FAILURE)

    with pytest.raises(GitRemoteUnavailableError):
        await repo.tip(ANON)

    assert not os.path.isdir(repo.path)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_short_budget")
async def test_resolve_retries_after_a_brief_outage(remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve`` (ls-remote in ``sync``) retries just like the writing calls."""
    repo = _repo(remote)
    calls = _fail_git(monkeypatch, "ls-remote", _DNS_FAILURE, times=1)

    sha = await repo.resolve(ANON)

    assert sha is not None
    assert calls.count("ls-remote") == 2, "exactly one retry"


# ---------------------------------------------------------------------------
# Warming up the platform repos at startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_platform_repos_warms_both_despite_one_failing(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken config only logs; the other platform repo is still cloned."""
    import opi.connectors.git_repo as module

    broken = {
        "url": str(tmp_path / "does-not-exist.git"),
        "branch": "main",
        "username": None,
        "password": None,
        "name": "deployments",
    }
    working = {"url": str(remote), "branch": "main", "username": None, "password": None, "name": "argo"}
    monkeypatch.setattr(module, "deployments_repo_config", lambda: broken)
    monkeypatch.setattr(module, "argo_repo_config", lambda: working)
    caplog.set_level("WARNING")

    await warm_platform_repos()

    assert any("niet vooraf klaarzetten" in record.message for record in caplog.records)
    assert os.path.isdir(_repo(remote).path), "the working repo was still warmed up"


@pytest.mark.asyncio
async def test_warm_platform_repos_survives_an_undecryptable_password(
    remote: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An AGE password without a key raises ValueError, which must only log as well."""
    import opi.connectors.git_repo as module

    broken = {"url": str(remote), "branch": "main", "username": "git", "password": "age:niettedecrypten"}
    working = {"url": str(remote), "branch": "main", "username": None, "password": None, "name": "argo"}
    monkeypatch.setattr(module, "deployments_repo_config", lambda: broken)
    monkeypatch.setattr(module, "argo_repo_config", lambda: working)
    caplog.set_level("WARNING")

    await warm_platform_repos()

    assert any("niet vooraf klaarzetten" in record.message for record in caplog.records)
    assert os.path.isdir(_repo(remote).path), "the second repo was still warmed up"
