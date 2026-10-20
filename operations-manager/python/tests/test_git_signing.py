"""OPI ondertekent zijn commits in de GitOps-repo's met een GPG-sleutel.

Tegen echte git en echte gpg, want een fake kan niet aantonen dat ``git
verify-commit`` de handtekening accepteert. Elke commitweg van de connector komt
langs: de werkmapweg (``commit_changes``), de plumbingweg (``build_commit``), de
initial commit van een leeg repo, en de commits die een rebase na een
push-conflict opnieuw maakt.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import GitConnector
from opi.core.config import settings
from opi.utils import git_signing
from opi.utils.git_signing import GitSigningKeyError, load_git_signing_identity

if TYPE_CHECKING:
    from pathlib import Path

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_GIT_ENV)
    return result.stdout.strip()


def _generate_key(tmp_path: Path) -> Path:
    """Een eigen sleutel zonder passphrase, zoals de sandboxtaak die aanmaakt."""
    home = tmp_path / "keygen"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--quiet",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Operations Manager <operations-manager@example.com>",
            "ed25519",
            "sign",
            "never",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    exported = subprocess.run(
        ["gpg", "--batch", "--quiet", "--armor", "--export-secret-keys"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    key_file = tmp_path / "signing-key.asc"
    key_file.write_text(exported.stdout)
    return key_file


@pytest.fixture
def signing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Een geladen signing identity, en na de test weer geen."""
    key_file = _generate_key(tmp_path)
    monkeypatch.setattr(settings, "GIT_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    identity = load_git_signing_identity()
    assert identity is not None
    yield identity.fingerprint
    monkeypatch.setattr(settings, "GIT_SIGNING_KEY_FILE", None)
    load_git_signing_identity()


def _verify(repo: Path, ref: str = "HEAD") -> str:
    """De uitvoer van ``git verify-commit``, met de keyring waarin OPI zijn sleutel zette."""
    identity = git_signing.get_git_signing_identity()
    assert identity is not None
    result = subprocess.run(
        ["git", "-C", str(repo), "verify-commit", "-v", ref],
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "GNUPGHOME": identity.gnupg_home},
    )
    assert result.returncode == 0, result.stderr
    return result.stderr


def _repo_with_one_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "bestand.txt").write_text("een\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _connector_on(repo: Path) -> GitConnector:
    conn = GitConnector(repo_url="https://example.invalid/repo.git", working_dir=str(repo), full_history=True)
    conn._repo_cloned = True
    conn._fetched_in_session = True
    return conn


# --- de sleutel inlezen ---------------------------------------------------------------


def test_zonder_geconfigureerde_sleutel_geen_identiteit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GIT_SIGNING_KEY_FILE", None)
    assert load_git_signing_identity() is None
    assert git_signing.get_git_signing_identity() is None


def test_ontbrekend_bestand_geen_identiteit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GIT_SIGNING_KEY_FILE", str(tmp_path / "bestaat-niet.asc"))
    assert load_git_signing_identity() is None


def test_geldige_sleutel_levert_fingerprint(signing_key: str) -> None:
    identity = git_signing.get_git_signing_identity()
    assert identity is not None
    assert identity.fingerprint == signing_key
    assert len(signing_key) == 40


def test_kapot_sleutelbestand_stopt_de_opstart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kapot = tmp_path / "kapot.asc"
    kapot.write_text("dit is geen gpg-sleutel\n")
    monkeypatch.setattr(settings, "GIT_SIGNING_KEY_FILE", str(kapot))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(GitSigningKeyError) as fout:
        load_git_signing_identity()

    assert str(kapot) in str(fout.value)


# --- de commitwegen -------------------------------------------------------------------


async def test_werkmapcommit_is_ondertekend(tmp_path: Path, signing_key: str) -> None:
    repo = _repo_with_one_commit(tmp_path)
    conn = _connector_on(repo)
    (repo / "bestand.txt").write_text("twee\n")

    await conn.commit_changes("een wijziging")

    assert signing_key[-16:] in _verify(repo)


async def test_plumbingcommit_is_ondertekend(tmp_path: Path, signing_key: str) -> None:
    repo = _repo_with_one_commit(tmp_path)
    conn = _connector_on(repo)

    commit = await conn.build_commit({"nieuw.txt": "inhoud\n"}, "via commit-tree")

    assert commit is not None
    assert signing_key[-16:] in _verify(repo, commit)


async def test_initial_commit_van_een_leeg_repo_is_ondertekend(tmp_path: Path, signing_key: str) -> None:
    repo = tmp_path / "leeg"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "andere")
    conn = _connector_on(repo)
    conn.branch = "main"

    await conn._ensure_correct_branch()

    assert signing_key[-16:] in _verify(repo)


async def test_rebase_na_een_conflict_ondertekent_opnieuw(tmp_path: Path, signing_key: str) -> None:
    """De commits die de rebase opnieuw maakt dragen ook een handtekening."""
    origin = _repo_with_one_commit(tmp_path)
    repo = tmp_path / "kloon"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))

    # De remote loopt vooruit op een ander bestand, dus de rebase slaagt zonder conflict.
    (origin / "ander.txt").write_text("remote\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "op de remote")

    conn = _connector_on(repo)
    conn.branch = "main"
    (repo / "bestand.txt").write_text("lokaal\n")
    await conn.commit_changes("lokale wijziging")
    voor_rebase = _git(repo, "rev-parse", "HEAD")

    assert await conn._rebase_on_remote("main") is True

    assert _git(repo, "rev-parse", "HEAD") != voor_rebase
    assert signing_key[-16:] in _verify(repo)


async def test_zonder_sleutel_blijft_de_commit_onondertekend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GIT_SIGNING_KEY_FILE", None)
    load_git_signing_identity()

    repo = _repo_with_one_commit(tmp_path)
    conn = _connector_on(repo)
    (repo / "bestand.txt").write_text("twee\n")

    await conn.commit_changes("een wijziging")

    assert _git(repo, "log", "-1", "--format=%G?") == "N"
