"""Tests voor het hergebruik van de git-connector per remote (RC-418).

"infrastructure" is vaak dezelfde repo, branch en pad als de deployments-repo
(``repositories[0]``): zonder hergebruik kost dat twee klonen en twee werkbomen voor
een repo die ook een keer gekloond kan worden. Twee REPO-NAMEN met dezelfde remote
moeten dus dezelfde connector krijgen, en die connector mag maar een keer dichtgaan.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.manager.project_manager import ProjectManager


def _manager() -> ProjectManager:
    manager = ProjectManager.__new__(ProjectManager)
    manager._ProjectManager__git_connectors_for_deployments = {}
    manager._ProjectManager__deployment_connectors_by_remote = {}
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
    with patch("opi.manager.project_manager.create_git_connector_from_repo_config", AsyncMock(side_effect=gemaakt)):
        infra = await manager.get_git_connector_for_deployment("infrastructure", _repo_config())
        deployments = await manager.get_git_connector_for_deployment("main-repo", _repo_config())

    assert infra is deployments
    assert infra is gemaakt[0], "de tweede naam had een nieuwe connector gekregen, niet de eerste hergebruikt"


@pytest.mark.asyncio
async def test_een_andere_remote_krijgt_zijn_eigen_connector() -> None:
    manager = _manager()
    gemaakt = [AsyncMock(), AsyncMock()]
    with patch("opi.manager.project_manager.create_git_connector_from_repo_config", AsyncMock(side_effect=gemaakt)):
        eerste = await manager.get_git_connector_for_deployment("main-repo", _repo_config())
        tweede = await manager.get_git_connector_for_deployment(
            "ander-repo", _repo_config(url="https://forgejo.example/een-andere-repo.git")
        )

    assert eerste is not tweede


@pytest.mark.asyncio
async def test_dezelfde_naam_nogmaals_opvragen_maakt_geen_tweede_connector() -> None:
    manager = _manager()
    with patch(
        "opi.manager.project_manager.create_git_connector_from_repo_config", AsyncMock(return_value=AsyncMock())
    ) as maak:
        await manager.get_git_connector_for_deployment("main-repo", _repo_config())
        await manager.get_git_connector_for_deployment("main-repo", _repo_config())

    assert maak.await_count == 1


@pytest.mark.asyncio
async def test_sluiten_raakt_de_gedeelde_connector_en_ruimt_beide_registers_op() -> None:
    """``close()`` op de echte GitConnector is idempotent (bewaakt met ``self._closed``),
    dus dat hij via beide registers aan de beurt komt kost op de echte connector niets;
    hier telt het alleen dat hij wel degelijk gesloten wordt en de registers leeg zijn."""
    manager = _manager()
    connector = AsyncMock()
    with patch("opi.manager.project_manager.create_git_connector_from_repo_config", AsyncMock(return_value=connector)):
        await manager.get_git_connector_for_deployment("infrastructure", _repo_config())
        await manager.get_git_connector_for_deployment("main-repo", _repo_config())

    await manager.close_git_connectors_for_deployments()

    connector.close.assert_awaited()
    assert manager._ProjectManager__git_connectors_for_deployments == {}
    assert manager._ProjectManager__deployment_connectors_by_remote == {}


@pytest.mark.asyncio
async def test_hetzelfde_pad_maar_een_andere_tak_is_geen_gedeelde_remote() -> None:
    """De connector lost bestandspaden op binnen zijn eigen werkboom; een andere branch
    is dus een andere remote-identiteit, ook met dezelfde URL."""
    manager = _manager()
    gemaakt = [AsyncMock(), AsyncMock()]
    with patch("opi.manager.project_manager.create_git_connector_from_repo_config", AsyncMock(side_effect=gemaakt)):
        main = await manager.get_git_connector_for_deployment("main-repo", _repo_config(branch="main"))
        andere_tak = await manager.get_git_connector_for_deployment("pr-tak", _repo_config(branch="pr-244"))

    assert main is not andere_tak
