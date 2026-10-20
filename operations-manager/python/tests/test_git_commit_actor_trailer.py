"""Tests for the Actor commit trailer (RC-284).

``_with_actor_trailer`` is covered directly as a pure function (message shapes:
single-line subject, an existing trailer block, a prose body, idempotency). The
real-git tests then prove the trailer actually survives into the three live commit
paths (``build_commit``, ``commit_changes``, and ``commit_and_push`` which is
``commit_changes`` plus a push) as something ``git log --format=%(trailers:...)``
can read back, and that the author/committer identity stays the fixed OPI one.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from opi.connectors.git import (
    COMMIT_ACTOR_TRAILER,
    GIT_COMMIT_AUTHOR_EMAIL,
    GIT_COMMIT_AUTHOR_NAME,
    GitConnector,
    _with_actor_trailer,
)
from opi.core.actor import Actor, ActorKind, set_actor, user_actor

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "projects").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "projects" / "alpha.yaml").write_text("name: alpha\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _connector_on(repo: Path) -> GitConnector:
    conn = GitConnector(repo_url="https://example.invalid/repo.git", working_dir=str(repo), full_history=True)
    conn._repo_cloned = True
    conn._fetched_in_session = True
    return conn


def _trailer_of(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "log", "-1", f"--format=%(trailers:key={COMMIT_ACTOR_TRAILER},valueonly)", ref)


def _author_of(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "log", "-1", "--format=%an <%ae>", ref)


# ---------------------------------------------------------------------------
# _with_actor_trailer: pure string shaping, no git involved
# ---------------------------------------------------------------------------


def test_subjectregel_zonder_body_krijgt_een_losse_alinea() -> None:
    """Een kale onderwerpregel is geen alinea om de trailer in te proppen."""
    set_actor(user_actor("iemand@example.org"))
    assert _with_actor_trailer("update alpha") == "update alpha\n\nActor: iemand@example.org"


def test_body_met_proza_krijgt_een_nieuwe_alinea() -> None:
    set_actor(user_actor("iemand@example.org"))
    message = "update alpha\n\nOmdat de limiet omhoog moest."
    assert _with_actor_trailer(message) == message + "\n\nActor: iemand@example.org"


def test_bestaand_trailerblok_krijgt_de_regel_direct_erbij() -> None:
    """De laatste alinea is al een trailerblok, dus geen extra witregel ervoor."""
    set_actor(user_actor("iemand@example.org"))
    message = "update alpha\n\nSigned-off-by: iemand-anders@example.org"
    assert _with_actor_trailer(message) == message + "\nActor: iemand@example.org"


def test_proza_die_op_een_trailer_lijkt_telt_niet_als_trailerblok() -> None:
    """Pas als ELKE regel van de laatste alinea een trailer is, telt hij als blok."""
    set_actor(user_actor("iemand@example.org"))
    message = "update alpha\n\nSigned-off-by: iemand-anders@example.org\nmaar dit is gewoon tekst"
    assert _with_actor_trailer(message) == message + "\n\nActor: iemand@example.org"


def test_al_aanwezige_trailer_wordt_niet_verdubbeld() -> None:
    set_actor(user_actor("iemand@example.org"))
    message = "update alpha\n\nActor: iemand@example.org"
    assert _with_actor_trailer(message) == message


def test_trailing_newlines_worden_genegeerd() -> None:
    set_actor(user_actor("iemand@example.org"))
    assert _with_actor_trailer("update alpha\n\n\n") == "update alpha\n\nActor: iemand@example.org"


def test_een_meegegeven_actor_wint_van_de_context() -> None:
    """De ProjectStore geeft de actor mee die hij ook logt, zodat log en trailer niet
    uit elkaar kunnen lopen."""
    set_actor(user_actor("context@example.org"))
    assert _with_actor_trailer("update alpha", user_actor("meegegeven@example.org")) == (
        "update alpha\n\nActor: meegegeven@example.org"
    )


def test_een_nieuwe_regel_in_de_actor_maakt_geen_tweede_trailer() -> None:
    """Het adres dat een federatie-peer meestuurt is de enige actorwaarde die van buiten
    komt; een nieuwe regel erin mag geen extra trailer worden."""
    set_actor(Actor(ActorKind.USER, "iemand@example.org\nSigned-off-by: niemand@example.org"))
    assert _with_actor_trailer("update alpha") == (
        "update alpha\n\nActor: iemand@example.org Signed-off-by: niemand@example.org"
    )


# ---------------------------------------------------------------------------
# De echte commitwegen: de trailer moet door git zelf uitleesbaar zijn
# ---------------------------------------------------------------------------


async def test_build_commit_draagt_de_actor_trailer(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)
    set_actor(user_actor("iemand@example.org"))

    commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nx: 1\n"}, "update alpha")
    assert commit is not None
    await conn.set_branch_ref(commit)

    assert _trailer_of(repo, commit) == "iemand@example.org"
    assert _author_of(repo, commit) == f"{GIT_COMMIT_AUTHOR_NAME} <{GIT_COMMIT_AUTHOR_EMAIL}>"


def _zonder_ambient_git_identiteit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``commit_changes`` zet de identiteit via ``git config`` (anders dan ``build_commit``,
    dat hem expliciet als env meegeeft), en een ambient GIT_AUTHOR_NAME/EMAIL in de
    omgeving wint het daarvan. Dit ontwikkelcontainer zelf heeft zo'n ambient identiteit
    (de git-identiteit van deze sessie), dus zonder dit zou de test de identiteit van de
    sessie zien in plaats van de vaste OPI-identiteit."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


async def test_commit_changes_draagt_de_actor_trailer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _zonder_ambient_git_identiteit(monkeypatch)
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)
    set_actor(user_actor("iemand@example.org"))

    (repo / "projects" / "alpha.yaml").write_text("name: alpha\nchanged: true\n")
    await conn.commit_changes("update alpha")

    assert _trailer_of(repo) == "iemand@example.org"
    assert _author_of(repo) == f"{GIT_COMMIT_AUTHOR_NAME} <{GIT_COMMIT_AUTHOR_EMAIL}>"


async def test_twee_commits_na_elkaar_dragen_elk_hun_eigen_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De trailer volgt de actor van het moment van committen, niet een vastgezette waarde."""
    _zonder_ambient_git_identiteit(monkeypatch)
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    set_actor(user_actor("eerste@example.org"))
    (repo / "projects" / "alpha.yaml").write_text("name: alpha\nstap: 1\n")
    await conn.commit_changes("stap een")
    eerste_commit = _git(repo, "rev-parse", "HEAD")

    set_actor(user_actor("tweede@example.org"))
    (repo / "projects" / "alpha.yaml").write_text("name: alpha\nstap: 2\n")
    await conn.commit_changes("stap twee")

    assert _trailer_of(repo, eerste_commit) == "eerste@example.org"
    assert _trailer_of(repo, "HEAD") == "tweede@example.org"
