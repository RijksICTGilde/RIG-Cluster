"""Een OTAP-verlaging via het portaal wordt gelogd, niet alleen via de API-route (RC-330).

``log_production_downgrades`` zelf is getoetst in ``test_project_environment.py`` en de
API-weg (``upsert_deployment``) in ``test_upsert_otap_verlaging.py``. Deze twee webwegen,
de wizard-edit (``_save_existing_project``) en de modal-edit
(``_process_and_save_modal_edit``), roepen de functie apart aan en hadden tot deze test
geen enkele dekking: een vergeten of verkeerd bedrade aanroep daar zou stilblijven.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.step_up import STEP_UP_SESSION_KEY

OWNER_EMAIL = "eigenaar@example.org"


def _productieproject(name: str = "demo") -> dict[str, Any]:
    return {
        "name": name,
        "display-name": "Demo",
        "deployments": [{"name": "d", "cluster": "c", "namespace": "n", "environment": "productie"}],
    }


# ---------------------------------------------------------------------------
# Wizard edit: _save_existing_project
# ---------------------------------------------------------------------------


def _patch_project_manager_voor_wizard(stored: dict[str, Any]):
    class _FakeProjectManager:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def get_contents(self) -> dict[str, Any]:
            return copy.deepcopy(stored)

        async def save_and_commit_project(self, project_data: dict[str, Any], _message: str, **_kwargs: Any) -> None:
            pass

        async def close(self) -> None:
            pass

    return patch("opi.manager.project_manager.ProjectManager", _FakeProjectManager)


def _request_for(email: str, project: str = "demo") -> Any:
    """Een verzoek met een VERSE step-up: sinds RC-332 weigert een verlaging zonder."""
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session={STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()},
        path_params={"project_name": project},
    )


def _patch_markeringsrecht(stored: dict[str, Any]):
    """De poort op ``environment.set``: de markering zetten of verlagen vraagt ops.

    Deze twee toetsen gaan over de MELDING, niet over het recht; de gebruiker is hier dus
    een platformbeheerder, die op elk project ops heeft.
    """
    from opi.services.project_service import ProjectSummary

    store = MagicMock()
    store.get.return_value = ProjectSummary(name=stored["name"], filename=f"{stored['name']}.yaml", data=stored)
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = True
    return (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
    )


@pytest.mark.asyncio
async def test_wizard_edit_meldt_een_verlaging_met_de_echte_gebruiker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from opi.web.router_wizard import _save_existing_project

    stored = _productieproject()

    with (
        patch("opi.web.router_wizard.authorize"),
        _patch_project_manager_voor_wizard(stored),
        patch("opi.web.router_wizard.clear_wizard_state"),
        caplog.at_level("WARNING", logger="opi.core.project_environment"),
    ):
        verlaagd = copy.deepcopy(stored["deployments"])
        verlaagd[0]["environment"] = "test"
        await _save_existing_project(_request_for(OWNER_EMAIL), stored["name"], {"deployments": verlaagd})

    meldingen = [record.getMessage() for record in caplog.records]
    assert any("deployment 'd'" in m and "project 'demo'" in m and OWNER_EMAIL in m for m in meldingen)


@pytest.mark.asyncio
async def test_wizard_edit_meldt_niets_als_productie_blijft(caplog: pytest.LogCaptureFixture) -> None:
    from opi.web.router_wizard import _save_existing_project

    stored = _productieproject()

    with (
        patch("opi.web.router_wizard.authorize"),
        _patch_project_manager_voor_wizard(stored),
        patch("opi.web.router_wizard.clear_wizard_state"),
        caplog.at_level("WARNING", logger="opi.core.project_environment"),
    ):
        await _save_existing_project(_request_for(OWNER_EMAIL), stored["name"], {"display-name": "Andere naam"})

    assert caplog.records == []


# ---------------------------------------------------------------------------
# Modal edit: _process_and_save_modal_edit
# ---------------------------------------------------------------------------


def _project_manager_voor_modal(stored: dict[str, Any]) -> MagicMock:
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=copy.deepcopy(stored))
    manager.save_and_commit_project = AsyncMock(return_value=None)
    return manager


@pytest.mark.asyncio
async def test_modal_edit_meldt_een_verlaging_met_de_echte_gebruiker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from opi.web.router_detail_edit import _process_and_save_modal_edit

    stored = _productieproject()
    na_wijziging = copy.deepcopy(stored)
    na_wijziging["deployments"][0]["environment"] = "test"

    flow = MagicMock()
    flow.flow_id = "modal-edit-identity"
    request = _request_for(OWNER_EMAIL)

    recht_store, recht_gebruikers = _patch_markeringsrecht(stored)
    with (
        recht_store,
        recht_gebruikers,
        patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value=na_wijziging)),
        patch("opi.web.router_detail_edit.extract_attachment_catalog", return_value={}),
        caplog.at_level("WARNING", logger="opi.core.project_environment"),
    ):
        await _process_and_save_modal_edit(
            request=request,
            project_manager=_project_manager_voor_modal(stored),
            project_name=stored["name"],
            flow=flow,
            wizard_token="token",
            merged_data={},
            active_sections=[],
            state=MagicMock(),
        )

    meldingen = [record.getMessage() for record in caplog.records]
    assert any("deployment 'd'" in m and "project 'demo'" in m and OWNER_EMAIL in m for m in meldingen)


@pytest.mark.asyncio
async def test_modal_edit_meldt_niets_zonder_verlaging(caplog: pytest.LogCaptureFixture) -> None:
    from opi.web.router_detail_edit import _process_and_save_modal_edit

    stored = _productieproject()
    flow = MagicMock()
    flow.flow_id = "modal-edit-identity"

    with (
        patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value=copy.deepcopy(stored))),
        patch("opi.web.router_detail_edit.extract_attachment_catalog", return_value={}),
        caplog.at_level("WARNING", logger="opi.core.project_environment"),
    ):
        await _process_and_save_modal_edit(
            request=_request_for(OWNER_EMAIL),
            project_manager=_project_manager_voor_modal(stored),
            project_name=stored["name"],
            flow=flow,
            wizard_token="token",
            merged_data={},
            active_sections=[],
            state=MagicMock(),
        )

    assert caplog.records == []
