"""Alleen bronnen van de allowlist worden gekloond.

Een helmfile- of chartbron komt integraal in de deploymentmap terecht en wordt daar door
de CMP gerenderd. Zonder allowlist bepaalt het projectbestand dus welke code het platform
rendert, en ``helmfile[].url`` kende geen enkele beperking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager
from opi.utils.render_sources import (
    is_allowed_render_source,
    normalize_git_url,
    path_inside_clone,
    require_allowed_render_source,
)

_TOEGESTAAN = "https://github.com/MinBZK/mijn-bureau-infra.git"


@pytest.fixture
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RENDER_SOURCE_ALLOWLIST", _TOEGESTAAN, raising=False)


def test_an_allowed_source_passes(allowlist: None) -> None:
    assert is_allowed_render_source(_TOEGESTAAN)
    assert is_allowed_render_source("https://github.com/MinBZK/mijn-bureau-infra")


def test_the_same_host_with_another_path_is_refused(allowlist: None) -> None:
    assert not is_allowed_render_source("https://github.com/MinBZK/iets-anders.git")


def test_a_path_that_only_starts_the_same_is_refused(allowlist: None) -> None:
    """Zonder padgrens zou een prefixvergelijking dit toelaten."""
    assert not is_allowed_render_source("https://github.com/MinBZK/mijn-bureau-infra-van-een-ander.git")


def test_userinfo_changes_nothing(allowlist: None) -> None:
    """De allowlist gaat over de bron, niet over wie hem ophaalt."""
    assert is_allowed_render_source("https://aanvaller:token@github.com/MinBZK/mijn-bureau-infra.git")


def test_an_empty_allowlist_allows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RENDER_SOURCE_ALLOWLIST", "", raising=False)

    assert not is_allowed_render_source(_TOEGESTAAN)


def test_the_refusal_names_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RENDER_SOURCE_ALLOWLIST", "", raising=False)

    with pytest.raises(ValueError, match="mijn-bureau-infra"):
        require_allowed_render_source(_TOEGESTAAN, "helmfile 'bureau'")


def test_the_refusal_does_not_repeat_a_token_from_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """``helmfile[].url`` kent geen aparte credentials, dus een private bron heeft een token
    in de userinfo. De melding bereikt de gebruiker en de logs."""
    monkeypatch.setattr(settings, "RENDER_SOURCE_ALLOWLIST", "", raising=False)

    with pytest.raises(ValueError, match="RENDER_SOURCE_ALLOWLIST") as fout:
        require_allowed_render_source("https://x-access-token:ghp_geheim@github.com/MinBZK/iets.git", "helmfile 'x'")

    assert "ghp_geheim" not in str(fout.value)
    assert "x-access-token" not in str(fout.value)
    assert "github.com/MinBZK/iets" in str(fout.value)


def test_dot_segments_cannot_walk_out_of_an_allowed_path(allowlist: None) -> None:
    """git vraagt dit pad op als ``/evil/repo.git``, dus de allowlist moet het zo wegen."""
    assert not is_allowed_render_source("https://github.com/MinBZK/mijn-bureau-infra/../../evil/repo.git")
    assert normalize_git_url("https://github.com/MinBZK/mijn-bureau-infra/../../evil/repo.git") == (
        "https://github.com/evil/repo"
    )


def test_ssh_shorthand_normalizes_to_an_ssh_url() -> None:
    assert (
        normalize_git_url("git@github.com:MinBZK/mijn-bureau-infra.git") == "ssh://github.com/MinBZK/mijn-bureau-infra"
    )


def _manager() -> Any:
    return MagicMock(spec=ProjectManager)


@pytest.mark.asyncio
async def test_clone_helm_chart_refuses_a_source_outside_the_allowlist(allowlist: None) -> None:
    """De aanroep in project_manager.py zelf: geen enkele clone voor een afgewezen bron."""
    with patch("opi.connectors.git.GitConnector") as git_connector_cls:
        with pytest.raises(ValueError, match="niet-toegestane-bron"):
            await ProjectManager._clone_helm_chart(
                _manager(),
                {"name": "demo", "git-url": "https://github.com/MinBZK/niet-toegestane-bron.git"},
                "/tmp/onbenut",
            )
        git_connector_cls.assert_not_called()


@pytest.mark.asyncio
async def test_clone_helmfile_source_refuses_a_source_outside_the_allowlist(allowlist: None) -> None:
    # Hier is GitConnector al op modulenaam gebonden (module-level import), in
    # tegenstelling tot _clone_helm_chart dat hem lokaal opnieuw importeert.
    with patch("opi.manager.project_manager.GitConnector") as git_connector_cls:
        with pytest.raises(ValueError, match="niet-toegestane-bron"):
            await ProjectManager._clone_helmfile_source(
                _manager(),
                {"name": "demo", "url": "https://github.com/MinBZK/niet-toegestane-bron.git"},
                "/tmp/onbenut",
            )
        git_connector_cls.assert_not_called()


@pytest.mark.asyncio
async def test_a_file_dependency_outside_the_clone_is_refused(allowlist: None, tmp_path: Path) -> None:
    """``Chart.yaml`` komt uit de gekloonde bron: een ``file://``-dependency die eruit
    wijst zou inhoud van de OPI-container naar de deploymentmap kopiëren."""

    def _connector(repo_url: str, branch: str, working_dir: str) -> Any:
        chart = Path(working_dir) / "chart"
        chart.mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "name: demo\ndependencies:\n  - name: gestolen\n    repository: file://../../../../etc\n"
        )
        connector = MagicMock()
        connector.clone = AsyncMock()
        connector.close = AsyncMock()
        return connector

    with (
        patch("opi.connectors.git.GitConnector", side_effect=_connector),
        pytest.raises(ValueError, match="buiten de gekloonde bron"),
    ):
        await ProjectManager._clone_helm_chart(
            _manager(),
            {"name": "demo", "git-url": _TOEGESTAAN, "chart-path": "chart"},
            str(tmp_path),
        )


@pytest.mark.parametrize("relative", ["../buiten", "/etc", "sub/../../buiten", "link"])
def test_a_path_outside_the_clone_is_refused(relative: str, tmp_path: Path) -> None:
    """``chart-path``, ``path`` en ``entry`` komen uit het projectbestand: zonder deze
    controle kopieert een ``..``, een absoluut pad of een symlink inhoud van de container
    naar de deploymentmap."""
    clone = tmp_path / "clone"
    (clone / "sub").mkdir(parents=True)
    (tmp_path / "buiten").mkdir()
    (clone / "link").symlink_to(tmp_path / "buiten")

    with pytest.raises(ValueError, match="buiten de gekloonde bron"):
        path_inside_clone(str(clone), relative, "helmfile 'demo'")


@pytest.mark.parametrize("relative", ["", ".", "chart", "sub/chart"])
def test_a_path_inside_the_clone_resolves(relative: str, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / "sub" / "chart").mkdir(parents=True)
    (clone / "chart").mkdir()

    resolved = path_inside_clone(str(clone), relative, "helmfile 'demo'")

    assert resolved.startswith(str(clone.resolve()))
