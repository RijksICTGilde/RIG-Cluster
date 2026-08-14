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

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import ProjectIntegrityError
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
        _, antwoord = await _process_and_save_modal_edit(
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
