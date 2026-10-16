"""RC-265: gelijktijdige schrijfacties op dezelfde projectmap in argo-applications.

Elke delete- en create-taak herschrijft ``<cluster>/<project>/kustomization.yaml`` en werkt
in zijn eigen kloon. Op 15 oktober 2026 strandden drie van acht gelijktijdige deletes in
nd-j7s op ``GitPushConflictError``, en om 18:45 ook een create. ``rebase_on_remote`` breekt
een conflict af en verwacht dat de aanroeper opnieuw toepast, maar op deze paden deed
niemand dat.

Deze tests draaien tegen een echte bare repo op schijf, omdat het bewijs juist in git zit:
alle commits landen, en de kustomization noemt precies de bestanden die er nog zijn.
"""

import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import GitConnector, _project_write_locks, _push_locks

if TYPE_CHECKING:
    from collections.abc import Callable

CLUSTER = "odcn-production"
PROJECT = "nd-j7s"


@pytest.fixture(autouse=True)
def _schone_locks():
    _push_locks.clear()
    _project_write_locks.clear()
    yield
    _push_locks.clear()
    _project_write_locks.clear()


@pytest.fixture(autouse=True)
def _file_url_wordt_begrepen(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitConnector kent ``file://`` niet; git wel, en dat is wat hier getoetst wordt."""
    echte = GitConnector._parse_git_url

    def _parse(self: GitConnector, url: str) -> dict:
        if url.startswith("file://"):
            return {"scheme": "file", "host": None, "port": None, "user": None, "path": url[7:], "needs_auth": False}
        return echte(self, url)

    monkeypatch.setattr(GitConnector, "_parse_git_url", _parse)


def _git(cwd: Path, *args: str) -> str:
    uitkomst = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    return uitkomst.stdout


def _kustomization(bestanden: list[str]) -> str:
    regels = "\n".join(f"  - {naam}" for naam in sorted(bestanden))
    return f"apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n{regels}\n"


def _herbouw_kustomization(projectmap: str) -> None:
    """Dezelfde vorm als ``ManifestGenerator.create_kustomization_files``: de inhoud van de
    map bepaalt de resources, dus hij noemt alleen wat er op dat moment staat."""
    aanwezig = [naam for naam in os.listdir(projectmap) if naam.endswith(".yaml") and naam != "kustomization.yaml"]
    Path(projectmap, "kustomization.yaml").write_text(_kustomization(aanwezig))


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Een bare repo met tien applicatiebestanden in één projectmap."""
    bare = tmp_path / "argo-applications.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(tmp_path, "clone", f"file://{bare}", str(seed))
    projectmap = seed / CLUSTER / PROJECT
    projectmap.mkdir(parents=True)
    for n in range(60, 70):
        (projectmap / f"pr-{n}.yaml").write_text(f"kind: Application\nmetadata:\n  name: {PROJECT}-pr-{n}\n")
    _herbouw_kustomization(str(projectmap))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "main")
    return bare


def _connector(remote: Path, werkmap: Path) -> GitConnector:
    werkmap.mkdir(parents=True, exist_ok=True)
    return GitConnector(
        repo_url=f"file://{remote}",
        branch="main",
        working_dir=str(werkmap),
        project_name=PROJECT,
        name="argo",
    )


async def _taak(
    remote: Path,
    tmp_path: Path,
    naam: str,
    bericht: str,
    mutatie: Callable[[str], None],
) -> None:
    """Eén taak met zijn eigen kloon, zoals een delete- of create-taak die krijgt."""
    connector = _connector(remote, tmp_path / f"werk-{naam}")
    await connector.ensure_repo_cloned()
    projectmap = os.path.join(await connector.get_working_dir(), CLUSTER, PROJECT)

    async def _mutate() -> None:
        mutatie(projectmap)
        _herbouw_kustomization(projectmap)

    await connector.mutate_and_push(bericht, _mutate)


def _verwijder(bestand: str) -> Callable[[str], None]:
    def _doe(projectmap: str) -> None:
        pad = os.path.join(projectmap, bestand)
        if os.path.exists(pad):
            os.remove(pad)

    return _doe


def _voeg_toe(bestand: str) -> Callable[[str], None]:
    def _doe(projectmap: str) -> None:
        Path(projectmap, bestand).write_text("kind: Application\nmetadata:\n  name: nieuw\n")

    return _doe


def _in_remote(remote: Path, pad: str) -> str:
    return _git(remote, "show", f"main:{pad}")


def _bestanden_in_remote(remote: Path) -> list[str]:
    uit = _git(remote, "ls-tree", "--name-only", f"main:{CLUSTER}/{PROJECT}")
    return sorted(naam for naam in uit.split() if naam.endswith(".yaml") and naam != "kustomization.yaml")


@pytest.mark.asyncio
async def test_acht_gelijktijdige_deletes_landen_alle_acht(remote: Path, tmp_path: Path) -> None:
    namen = [f"pr-{n}.yaml" for n in range(60, 68)]

    await asyncio.gather(
        *(_taak(remote, tmp_path, naam, f"Delete ArgoCD application for '{naam}'", _verwijder(naam)) for naam in namen)
    )

    log = _git(remote, "log", "--format=%s", "main")
    for naam in namen:
        assert f"Delete ArgoCD application for '{naam}'" in log, f"de commit van {naam} staat niet op de remote"

    assert _bestanden_in_remote(remote) == ["pr-68.yaml", "pr-69.yaml"]
    assert _in_remote(remote, f"{CLUSTER}/{PROJECT}/kustomization.yaml") == _kustomization(
        ["pr-68.yaml", "pr-69.yaml"]
    ), "de kustomization noemt niet precies de overgebleven bestanden"


@pytest.mark.asyncio
async def test_een_create_en_een_delete_tegelijk_in_een_project(remote: Path, tmp_path: Path) -> None:
    await asyncio.gather(
        _taak(remote, tmp_path, "create", "Added ArgoCD resources for project nd-j7s", _voeg_toe("pr-70.yaml")),
        _taak(remote, tmp_path, "delete", "Delete ArgoCD application for 'pr-69.yaml'", _verwijder("pr-69.yaml")),
    )

    log = _git(remote, "log", "--format=%s", "main")
    assert "Added ArgoCD resources for project nd-j7s" in log
    assert "Delete ArgoCD application for 'pr-69.yaml'" in log

    bestanden = _bestanden_in_remote(remote)
    assert "pr-70.yaml" in bestanden, "de create is verdwenen achter de delete"
    assert "pr-69.yaml" not in bestanden
    assert _in_remote(remote, f"{CLUSTER}/{PROJECT}/kustomization.yaml") == _kustomization(bestanden)


@pytest.mark.asyncio
async def test_de_mutatie_ziet_de_stand_van_de_vorige_taak(remote: Path, tmp_path: Path) -> None:
    """De kern: binnen de lock gaat ``reset_to_remote`` voor, dus de tweede taak beslist op
    wat de eerste net gepusht heeft en niet op zijn eigen kloon van daarvoor."""
    gezien: list[list[str]] = []

    def _noteer_en_verwijder(bestand: str) -> Callable[[str], None]:
        def _doe(projectmap: str) -> None:
            gezien.append(_bestanden_in_remote(remote))
            _verwijder(bestand)(projectmap)

        return _doe

    namen = [f"pr-{n}.yaml" for n in range(60, 64)]
    await asyncio.gather(
        *(_taak(remote, tmp_path, naam, f"delete {naam}", _noteer_en_verwijder(naam)) for naam in namen)
    )

    aantallen = [len(stand) for stand in gezien]
    assert aantallen == [10, 9, 8, 7], f"elke taak hoort de vorige verwijdering te zien, zag {aantallen}"
