"""De commit die de audit noemt moet in de echte repo terug te vinden zijn.

Dit is waarom de audit zonder een if-then-else per route kan: de rij draagt de SHA, en
``git show <sha>`` geeft de wijziging. Daarom hier echte git en geen nagemaakte
connector: dat de store een tekenreeks doorgeeft bewijst niets over een commit die
bestaat (de rest van het spoor staat in ``tests/test_project_store.py``).
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

from opi.connectors.git import GitConnector
from opi.core.actor import user_actor
from opi.core.audit_trail import begin_trail, trail
from opi.services.project_service import get_project_service
from opi.services.project_store import GitProjectStore
from opi.utils.yaml_util import dump_yaml_to_string

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
    return result.stdout


def _project() -> dict[str, Any]:
    return {
        "schema-version": "2.12",
        "name": "demo",
        "users": [{"email": "beheerder@rijksoverheid.nl", "roles": ["admin"]}],
        "clusters": ["odcn-production"],
        "config": {},
    }


async def test_git_show_van_de_vastgelegde_sha_bevat_de_wijziging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_project_service().clear_all_projects()
    begin_trail()

    # Een kale remote plus een klone ervan: de store publiceert met een push, en zonder
    # remote valt die weg en daarmee de hele weg naar de SHA.
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-q", "-b", "main")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, env=_GIT_ENV)
    (work / "projects").mkdir()
    (work / "projects" / "demo.yaml").write_text(dump_yaml_to_string(_project()))
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "initial")
    _git(work, "push", "-q", "origin", "main")

    store = GitProjectStore(working_dir=str(work))
    # De URL is alleen de sleutel van het pushslot; de push zelf gaat naar ``origin``, en
    # dat is de kale remote hierboven. Een pad als URL kent de connector niet.
    connector = GitConnector(repo_url="https://example.invalid/zad-projects.git", working_dir=str(work))
    connector._repo_cloned = True
    connector._fetched_in_session = True

    async def _get_connector() -> GitConnector:
        return connector

    monkeypatch.setattr(store, "get_connector", _get_connector)

    gewijzigd = _project()
    gewijzigd["description"] = "een nieuwe omschrijving"
    await store.save(
        "demo",
        gewijzigd,
        message="zet omschrijving",
        actor=user_actor("beheerder@rijksoverheid.nl"),
        enforce_validation=False,
    )

    shas = trail().commit_shas
    assert len(shas) == 1, shas
    assert "een nieuwe omschrijving" in _git(work, "show", shas[0])
