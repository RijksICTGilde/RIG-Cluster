"""Tests voor het hergebruik van de werkmap per remote (RC-418, RC-422).

"infrastructure" is vaak dezelfde repo, branch en pad als de deployments-repo
(``repositories[0]``): zonder hergebruik zijn dat twee tempmappen met dezelfde scope,
en dan landt de generatie van de twee niet in een commit. Twee REPO-NAMEN met dezelfde
remote moeten dus dezelfde werkmap krijgen, en die werkmap mag maar een keer dichtgaan.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager


def _manager() -> ProjectManager:
    manager = ProjectManager.__new__(ProjectManager)
    manager._ProjectManager__deployment_workspaces = {}
    manager._ProjectManager__deployment_workspaces_by_remote = {}
    manager._ProjectManager__scratch_dirs = []
    manager.get_name = AsyncMock(return_value="demo")
    return manager


def _repo_config(**overrides) -> dict:
    config = {"url": "https://forgejo.example/zad-deployments.git", "branch": "main", "path": "."}
    config.update(overrides)
    return config


@pytest.mark.asyncio
async def test_twee_namen_met_dezelfde_remote_krijgen_dezelfde_connector() -> None:
    manager = _manager()
    gemaakt = [AsyncMock(), AsyncMock()]
    with patch("opi.manager.project_manager.workspace_from_repo_config", MagicMock(side_effect=gemaakt)):
        infra = await manager.get_deployment_workspace("infrastructure", _repo_config())
        deployments = await manager.get_deployment_workspace("main-repo", _repo_config())

    assert infra is deployments
    assert infra is gemaakt[0], "de tweede naam had een nieuwe werkmap gekregen, niet de eerste hergebruikt"


@pytest.mark.asyncio
async def test_een_andere_remote_krijgt_zijn_eigen_connector() -> None:
    manager = _manager()
    gemaakt = [AsyncMock(), AsyncMock()]
    with patch("opi.manager.project_manager.workspace_from_repo_config", MagicMock(side_effect=gemaakt)):
        eerste = await manager.get_deployment_workspace("main-repo", _repo_config())
        tweede = await manager.get_deployment_workspace(
            "ander-repo", _repo_config(url="https://forgejo.example/een-andere-repo.git")
        )

    assert eerste is not tweede


@pytest.mark.asyncio
async def test_dezelfde_naam_nogmaals_opvragen_maakt_geen_tweede_connector() -> None:
    manager = _manager()
    with patch("opi.manager.project_manager.workspace_from_repo_config", MagicMock(return_value=AsyncMock())) as maak:
        await manager.get_deployment_workspace("main-repo", _repo_config())
        await manager.get_deployment_workspace("main-repo", _repo_config())

    assert maak.call_count == 1


@pytest.mark.asyncio
async def test_sluiten_raakt_de_gedeelde_connector_en_ruimt_beide_registers_op() -> None:
    """``close()`` op de echte GitConnector is idempotent (bewaakt met ``self._closed``),
    dus dat hij via beide registers aan de beurt komt kost op de echte connector niets;
    hier telt het alleen dat hij wel degelijk gesloten wordt en de registers leeg zijn."""
    manager = _manager()
    connector = AsyncMock()
    with patch("opi.manager.project_manager.workspace_from_repo_config", MagicMock(return_value=connector)):
        await manager.get_deployment_workspace("infrastructure", _repo_config())
        await manager.get_deployment_workspace("main-repo", _repo_config())

    await manager.close_deployment_workspaces()

    connector.close.assert_awaited()
    assert manager._ProjectManager__deployment_workspaces == {}
    assert manager._ProjectManager__deployment_workspaces_by_remote == {}


@pytest.mark.asyncio
async def test_hetzelfde_pad_maar_een_andere_tak_is_geen_gedeelde_remote() -> None:
    """De werkmap hoort bij een branch; een andere branch is dus een andere
    remote-identiteit, ook met dezelfde URL."""
    manager = _manager()
    gemaakt = [AsyncMock(), AsyncMock()]
    with patch("opi.manager.project_manager.workspace_from_repo_config", MagicMock(side_effect=gemaakt)):
        main = await manager.get_deployment_workspace("main-repo", _repo_config(branch="main"))
        andere_tak = await manager.get_deployment_workspace("pr-tak", _repo_config(branch="pr-244"))

    assert main is not andere_tak


# ---------------------------------------------------------------------------
# materialize_cluster_tree: de alleen-lezen wegwerpmap voor de lezers over alle projecten
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_cluster_tree_zet_de_clusterboom_in_een_wegwerpmap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De scope is afgeleid van CLUSTER_MANAGER en het repository-pad, niet van het project:
    dit is de boom van ALLE projecten, alleen om te lezen."""
    manager = _manager()
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "odcn-production")
    repo = AsyncMock()
    repo.tip = AsyncMock(return_value="sha123")
    repo.materialize = AsyncMock(return_value=["infra/odcn-production"])

    with (
        patch("opi.manager.project_manager.get_git_repo", MagicMock(return_value=repo)),
        patch("opi.manager.project_manager.credentials_from_repo_config", MagicMock(return_value="creds")),
    ):
        pad = await manager.materialize_cluster_tree(_repo_config(path="infra"))

    try:
        scratch = manager._ProjectManager__scratch_dirs[0]
        assert pad == os.path.join(scratch, "infra")
        repo.materialize.assert_awaited_once_with(["infra/odcn-production"], "sha123", scratch, "creds")
    finally:
        await manager.close_deployment_workspaces()


@pytest.mark.asyncio
async def test_materialize_cluster_tree_zonder_repo_path_geeft_de_wegwerpmap_zelf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "odcn-production")
    repo = AsyncMock()
    repo.tip = AsyncMock(return_value="sha123")
    repo.materialize = AsyncMock(return_value=["odcn-production"])

    with (
        patch("opi.manager.project_manager.get_git_repo", MagicMock(return_value=repo)),
        patch("opi.manager.project_manager.credentials_from_repo_config", MagicMock(return_value="creds")),
    ):
        pad = await manager.materialize_cluster_tree(_repo_config(path=""))

    try:
        assert pad == manager._ProjectManager__scratch_dirs[0]
    finally:
        await manager.close_deployment_workspaces()


@pytest.mark.asyncio
async def test_materialize_cluster_tree_geeft_none_als_er_niets_stond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De cluster-map bestaat (nog) niet op deze remote: niets om te lezen."""
    manager = _manager()
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "odcn-production")
    repo = AsyncMock()
    repo.tip = AsyncMock(return_value="sha123")
    repo.materialize = AsyncMock(return_value=[])

    with (
        patch("opi.manager.project_manager.get_git_repo", MagicMock(return_value=repo)),
        patch("opi.manager.project_manager.credentials_from_repo_config", MagicMock(return_value="creds")),
    ):
        pad = await manager.materialize_cluster_tree(_repo_config(path="infra"))

    try:
        assert pad is None
    finally:
        await manager.close_deployment_workspaces()


@pytest.mark.asyncio
async def test_materialize_cluster_tree_weigert_een_pad_buiten_de_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Een repository-pad met ``..`` mag de inventaris niet over een map buiten de repo
    laten oordelen; dit geeft None in plaats van de fout door te laten bubbelen."""
    manager = _manager()
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "odcn-production")
    repo = AsyncMock()

    with (
        patch("opi.manager.project_manager.get_git_repo", MagicMock(return_value=repo)),
        patch("opi.manager.project_manager.credentials_from_repo_config", MagicMock(return_value="creds")),
    ):
        pad = await manager.materialize_cluster_tree(_repo_config(path="a/../../../etc"))

    assert pad is None
    repo.materialize.assert_not_awaited()
    assert manager._ProjectManager__scratch_dirs == []
