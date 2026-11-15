"""Een gelijktijdige wijziging in een bewerkmodal is geen 500.

Gemeten in de reallife-doorloop van RC-112: terwijl de componenten-modal een component
verwijderde, patchte een API-taak een ander component in hetzelfde projectbestand. De
compare-and-swap in de store zag dat als een botsing op hetzelfde onderdeel en gooide
``ConflictError``, die niemand ving - dus kreeg de browser een kale 500 op
``POST /projects/<naam>/modal-wizard/.../step/components-edit``.

De uitleg zat al in de fout zelf ("Project is gewijzigd sinds je begon met bewerken ...").
Deze test pint vast dat die uitleg de gebruiker bereikt via dezelfde weg als een
validatiefout - de review met ``global_errors`` - en niet als een onbehandelde uitzondering.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.project_schema import ProjectIntegrityError
from opi.services.project_store import ConflictError
from opi.web.router_detail_edit import _process_and_save_modal_edit

CONFLICT_TEKST = "Project 'demo' is gewijzigd sinds je begon met bewerken"


def _project_manager(fout: Exception) -> MagicMock:
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value={"name": "demo", "components": []})
    manager.save_and_commit_project = AsyncMock(side_effect=fout)
    return manager


async def _save_met(fout: Exception) -> tuple[Any, list[str]]:
    """Draai de opslagstap met een falende save en geef (antwoord, global_errors) terug."""
    flow = MagicMock()
    flow.flow_id = "modal-edit-components"
    opgevangen: list[str] = []

    def _review(*args: Any, **kwargs: Any) -> str:
        opgevangen.extend(kwargs.get("global_errors") or [])
        return "review"

    with (
        patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value={"name": "demo"})),
        patch("opi.web.router_detail_edit.extract_attachment_catalog", return_value={}),
        patch("opi.web.router_detail_edit._render_modal_review", side_effect=_review),
    ):
        antwoord = await _process_and_save_modal_edit(
            request=MagicMock(),
            project_manager=_project_manager(fout),
            project_name="demo",
            flow=flow,
            wizard_token="token",
            merged_data={},
            active_sections=[],
            state=MagicMock(),
        )
    return antwoord, opgevangen


@pytest.mark.asyncio
async def test_conflict_wordt_de_review_met_uitleg_en_geen_500() -> None:
    """Een ConflictError uit de store komt als melding terug, niet als uitzondering."""
    antwoord, global_errors = await _save_met(ConflictError(CONFLICT_TEKST))

    assert antwoord == "review", "een botsing moet de review met een melding opleveren"
    assert global_errors == [CONFLICT_TEKST]


@pytest.mark.asyncio
async def test_validatiefout_blijft_precies_zo_werken() -> None:
    """De bestaande weg voor een validatiefout verandert niet door de toevoeging."""
    antwoord, global_errors = await _save_met(ProjectIntegrityError("component 'web' bestaat niet"))

    assert antwoord == "review"
    assert global_errors == ["component 'web' bestaat niet"]


@pytest.mark.asyncio
async def test_een_onbekende_fout_wordt_nog_steeds_niet_ingeslikt() -> None:
    """Alleen deze drie soorten worden opgevangen; de rest hoort door te vallen.

    Anders zou de volgende ronde 'geen 500 meer' verwarren met 'geen fout meer'.
    """
    with pytest.raises(RuntimeError):
        await _save_met(RuntimeError("iets heel anders"))


@pytest.mark.asyncio
async def test_the_wizard_rerenders_a_conflict_instead_of_500() -> None:
    """RC-483 P4: the edit wizard gave a bare 500 where the modal gave a message.

    ``ConflictError`` fell through the ``except Exception: raise`` of ``_do_submit``, so the
    user got an error page and lost their input. They belong back on their step with the
    store's explanation, as in the modal.
    """
    from opi.forms.editables.processor import EditableFormProcessor
    from opi.web.router_wizard import _do_submit

    message = "Dit project is ondertussen door iemand anders gewijzigd."

    state = MagicMock()
    state.flow_id = "edit-project"
    state.project_name = "demo"
    state.current_step = "identity"
    state.step_data = {}
    state.staged_attachments = {}
    state.is_edit = True
    state.get_merged_data.return_value = {"name": "demo", "display-name": "Demo"}

    seen: dict[str, Any] = {}

    def _render(_request: Any, *, template: str, context: dict[str, Any]) -> Any:
        seen.update(context)
        return SimpleNamespace(template_name=template)

    request = SimpleNamespace(
        state=SimpleNamespace(user={"email": "owner@example.com"}, csrf_token="t"),
        query_params={},
        cookies={},
    )

    with (
        patch("opi.web.router_wizard.get_wizard_state", return_value=state),
        patch("opi.web.router_wizard.save_wizard_state"),
        patch.object(
            EditableFormProcessor,
            "process_json_submission",
            new=AsyncMock(return_value=({"name": "demo", "display-name": "Demo"}, {})),
        ),
        patch.object(EditableFormProcessor, "enforce_sections", new=AsyncMock(return_value=[])),
        patch("opi.forms.editables.lifecycle.run_hooks", new=AsyncMock()),
        patch("opi.web.router_wizard._save_existing_project", side_effect=ConflictError(message)),
        patch("opi.web.router_wizard.render", side_effect=_render),
    ):
        await _do_submit(request, "edit-project")

    assert seen["global_errors"] == [message]
    assert seen["errors"] == {}
