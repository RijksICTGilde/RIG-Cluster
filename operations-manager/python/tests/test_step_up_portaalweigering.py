"""Het portaal WEIGERT een handeling in productie zonder verse step-up (RC-332).

De succespaden staan in ``test_otap_verlaging_webwegen.py`` (de melding) en
``test_delete_routes_step_up.py`` (de verwijderroutes). Hier staat de andere helft van de
toets uit plan-stap 4: zonder step-up gebeurt er niets, en de weigering is te lezen in de
log. Dat gat liet een replay naar de verkeerde route onzichtbaar blijven.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.auth_decorators import requires_step_up
from opi.core.step_up import STEP_UP_SESSION_KEY, StepUpAction

OWNER_EMAIL = "eigenaar@example.org"
TOKEN = "wt-123"


def _productieproject() -> dict[str, Any]:
    return {
        "name": "demo",
        "display-name": "Demo",
        "deployments": [{"name": "d", "cluster": "c", "namespace": "n", "environment": "productie"}],
        "components": [{"name": "c1"}],
    }


def _request(*, verse_step_up: bool) -> Any:
    sessie = {STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()} if verse_step_up else {}
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": OWNER_EMAIL}),
        session=sessie,
        method="POST",
        url=SimpleNamespace(path="/projects/demo/modal-wizard/modal-edit-identity/step/identity"),
        headers={},
        path_params={"project_name": "demo"},
    )


def _markeringsrecht():
    """De poort op ``environment.set``: de markering zetten of verlagen vraagt ops.

    Deze toetsen gaan over de STEP-UP en niet over het recht, dus de gebruiker is hier
    een platformbeheerder; die heeft ops op elk project, los van de inhoud ervan.
    """
    from opi.services.project_service import ProjectSummary

    store = MagicMock()
    store.get.return_value = ProjectSummary(name="demo", filename="demo.yaml", data={"name": "demo"})
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = True
    return (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
    )


def _project_manager(stored: dict[str, Any]) -> MagicMock:
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=copy.deepcopy(stored))
    manager.save_and_commit_project = AsyncMock(return_value=None)
    return manager


async def _modal_edit(
    *,
    na_wijziging: dict[str, Any],
    verse_step_up: bool,
    manager: MagicMock,
    dialoog: AsyncMock,
) -> Any:
    from opi.web.router_detail_edit import _process_and_save_modal_edit

    flow = MagicMock()
    flow.flow_id = "modal-edit-identity"
    recht_store, recht_gebruikers = _markeringsrecht()

    with (
        recht_store,
        recht_gebruikers,
        patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value=na_wijziging)),
        patch("opi.web.router_detail_edit.extract_attachment_catalog", return_value={}),
        patch("opi.web.router_detail_edit.render_step_up_dialog", new=dialoog),
        patch("opi.web.router_detail_edit.step_up_dialog_response", side_effect=lambda html: html),
    ):
        _, response = await _process_and_save_modal_edit(
            request=_request(verse_step_up=verse_step_up),
            project_manager=manager,
            project_name="demo",
            flow=flow,
            wizard_token=TOKEN,
            merged_data={},
            active_sections=[],
            state=MagicMock(),
        )
    return response


# ---------------------------------------------------------------------------
# Modal-edit: de opslagtrechter van de projectpagina
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modal_edit_zonder_step_up_slaat_niets_op_en_meldt_de_weigering(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stored = _productieproject()
    verlaagd = copy.deepcopy(stored)
    verlaagd["deployments"][0]["environment"] = "acceptatie"
    manager = _project_manager(stored)
    dialoog = AsyncMock(return_value="<dialoog/>")

    with caplog.at_level("WARNING", logger="opi.core.step_up"):
        response = await _modal_edit(na_wijziging=verlaagd, verse_step_up=False, manager=manager, dialoog=dialoog)

    assert response == "<dialoog/>"
    manager.save_and_commit_project.assert_not_awaited()
    meldingen = [record.getMessage() for record in caplog.records]
    assert any("Geweigerd, geen verse step-up" in m and "demo" in m and OWNER_EMAIL in m for m in meldingen)


@pytest.mark.asyncio
async def test_modal_edit_laat_de_herhaling_naar_de_confirm_route_wijzen() -> None:
    """De step-route eist een JSON-body; de bodyloze herhaling moet dus naar /confirm."""
    stored = _productieproject()
    verlaagd = copy.deepcopy(stored)
    verlaagd["deployments"][0]["environment"] = "acceptatie"
    dialoog = AsyncMock(return_value="<dialoog/>")

    await _modal_edit(na_wijziging=verlaagd, verse_step_up=False, manager=_project_manager(stored), dialoog=dialoog)

    dialoog.assert_awaited_once_with(
        ANY,
        vervolg=f"/projects/demo/modal-wizard/modal-edit-identity/confirm?_wizard_token={TOKEN}",
        handeling=StepUpAction.MARKING_SET.phrase,
        vervolg_token=TOKEN,
    )


@pytest.mark.asyncio
async def test_modal_edit_met_verse_step_up_slaat_de_verlaging_wel_op() -> None:
    stored = _productieproject()
    verlaagd = copy.deepcopy(stored)
    verlaagd["deployments"][0]["environment"] = "acceptatie"
    manager = _project_manager(stored)
    dialoog = AsyncMock(return_value="<dialoog/>")

    response = await _modal_edit(na_wijziging=verlaagd, verse_step_up=True, manager=manager, dialoog=dialoog)

    assert response is None
    dialoog.assert_not_awaited()
    manager.save_and_commit_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_modal_edit_needs_nothing_for_a_switch_outside_production() -> None:
    """Van test naar ontwikkeling is een deployment schrijven, geen markering zetten: alleen
    een overgang van of naar productie vraagt de step-up."""
    stored = _productieproject()
    stored["deployments"][0]["environment"] = "test"
    na = copy.deepcopy(stored)
    na["deployments"][0]["environment"] = "ontwikkeling"
    manager = _project_manager(stored)
    dialoog = AsyncMock(return_value="<dialoog/>")

    response = await _modal_edit(na_wijziging=na, verse_step_up=False, manager=manager, dialoog=dialoog)

    assert response is None
    dialoog.assert_not_awaited()
    manager.save_and_commit_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_modal_edit_zonder_markeringswijziging_vraagt_niets() -> None:
    """Een gewone bewerking op een productieproject gaat door: de poort staat op de
    markering en op het verwijderen, niet op elke opslag."""
    stored = _productieproject()
    na = copy.deepcopy(stored)
    na["display-name"] = "Andere naam"
    manager = _project_manager(stored)
    dialoog = AsyncMock(return_value="<dialoog/>")

    response = await _modal_edit(na_wijziging=na, verse_step_up=False, manager=manager, dialoog=dialoog)

    assert response is None
    dialoog.assert_not_awaited()
    manager.save_and_commit_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_modal_edit_zonder_step_up_weigert_ook_een_component_uit_productie(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stored = _productieproject()
    stored["deployments"][0]["components"] = [{"reference": "c1"}]
    zonder_component = copy.deepcopy(stored)
    zonder_component["components"] = []
    manager = _project_manager(stored)
    dialoog = AsyncMock(return_value="<dialoog/>")

    with caplog.at_level("WARNING", logger="opi.core.step_up"):
        await _modal_edit(na_wijziging=zonder_component, verse_step_up=False, manager=manager, dialoog=dialoog)

    manager.save_and_commit_project.assert_not_awaited()
    assert dialoog.await_args.kwargs["handeling"] == "het component 'c1' te verwijderen"
    assert any("te verwijderen" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Wizard-edit: dezelfde weigering, maar als kale 403
# ---------------------------------------------------------------------------


def _patch_wizard_manager(stored: dict[str, Any], opslag: list[dict[str, Any]]):
    class _FakeProjectManager:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def get_contents(self) -> dict[str, Any]:
            return copy.deepcopy(stored)

        async def save_and_commit_project(self, project_data: dict[str, Any], _message: str, **_kwargs: Any) -> None:
            opslag.append(project_data)

        async def close(self) -> None:
            pass

    return patch("opi.manager.project_manager.ProjectManager", _FakeProjectManager)


@pytest.mark.asyncio
async def test_wizard_edit_zonder_step_up_geeft_403_en_slaat_niets_op(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from opi.web.router_wizard import _save_existing_project

    stored = _productieproject()
    opslag: list[dict[str, Any]] = []
    verlaagd = copy.deepcopy(stored["deployments"])
    verlaagd[0]["environment"] = "acceptatie"

    with (
        patch("opi.web.router_wizard.authorize"),
        _patch_wizard_manager(stored, opslag),
        patch("opi.web.router_wizard.clear_wizard_state"),
        caplog.at_level("WARNING", logger="opi.core.step_up"),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _save_existing_project(_request(verse_step_up=False), "demo", {"deployments": verlaagd})

    assert excinfo.value.status_code == 403
    assert "verse code uit je 2-factor" in excinfo.value.detail
    assert opslag == []
    assert any("Geweigerd, geen verse step-up" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# De decorator: de weigering en het gebruik staan in de log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decorator_meldt_de_weigering_als_warning(caplog: pytest.LogCaptureFixture) -> None:
    @requires_step_up(lambda request, **kwargs: True, actie=StepUpAction.DEPLOYMENT_DELETE)
    async def route(request: Any, project_name: str, deployment_name: str) -> str:
        return "uitgevoerd"

    with (
        caplog.at_level("WARNING", logger="opi.core.step_up"),
        pytest.raises(HTTPException),
    ):
        await route(_request(verse_step_up=False), project_name="demo", deployment_name="prod")

    melding = caplog.records[0].getMessage()
    assert "deze deployment te verwijderen" in melding
    assert "doel 'prod'" in melding
    assert "project 'demo'" in melding
    assert OWNER_EMAIL in melding


@pytest.mark.asyncio
async def test_decorator_meldt_een_gebruikte_step_up_als_info(caplog: pytest.LogCaptureFixture) -> None:
    @requires_step_up(lambda request, **kwargs: True, actie=StepUpAction.PROJECT_DELETE)
    async def route(request: Any, project_name: str) -> str:
        return "uitgevoerd"

    with caplog.at_level("INFO", logger="opi.core.auth_decorators"):
        assert await route(_request(verse_step_up=True), project_name="demo") == "uitgevoerd"

    meldingen = [record.getMessage() for record in caplog.records]
    assert any("Verse step-up gebruikt om dit project te verwijderen" in m and OWNER_EMAIL in m for m in meldingen)


# ---------------------------------------------------------------------------
# Het fragment dat de handeling herhaalt
# ---------------------------------------------------------------------------


def _render_herhaal(vervolg_token: str, velden: dict[str, str] | None = None) -> str:
    """Het fragment kaal door Jinja, zonder de LOTC-laag: alleen de htmx-attributen.

    ``_vals`` stelt de body samen, zoals ``render_step_up_repeat`` dat doet: het token en
    de velden van de handeling gaan langs dezelfde weg mee.
    """
    from jinja2 import Environment, FileSystemLoader
    from opi.web.step_up_dialog import _vals

    # autoescape=True zoals in productie (opi/core/templates_lotc.py:82). select_autoescape
    # kijkt naar de extensie en kent .j2 niet, dus dat gaf hier een escape-loze render.
    env = Environment(loader=FileSystemLoader("opi/templates_lotc"), autoescape=True)
    template = env.get_template("bg/_step-up-herhalen.html.j2")
    return template.render(
        request=SimpleNamespace(state=SimpleNamespace(csrf_token="csrf")),
        vervolg="/projects/demo/modal-wizard/modal-edit-identity/confirm",
        vals=_vals(vervolg_token or None, velden),
    )


def test_de_herhaling_stuurt_het_wizard_token_in_de_body_mee() -> None:
    html = _render_herhaal("wt-123")
    assert 'hx-post="/projects/demo/modal-wizard/modal-edit-identity/confirm"' in html
    assert "_wizard_token" in html
    assert "wt-123" in html


def test_de_herhaling_zonder_token_stuurt_een_lege_body() -> None:
    assert "_wizard_token" not in _render_herhaal("")


def test_de_herhaling_stuurt_de_velden_van_de_handeling_mee() -> None:
    """De dialoog verving het formulier, dus de velden rijden mee (RC-387)."""
    html = _render_herhaal("", {"dagen": "30"})
    assert "dagen" in html
    assert "30" in html
