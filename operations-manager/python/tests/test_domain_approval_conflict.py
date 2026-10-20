"""Een gelijktijdige wijziging tijdens een domeinbesluit is geen 500.

Hetzelfde geval als in ``test_modal_edit_conflict.py``, maar op het pad van het
domeinbesluit: terwijl een beheerder een subdomein goedkeurt, schrijft een API-taak in
hetzelfde projectbestand. De compare-and-swap in de store ziet dat als een botsing op
hetzelfde onderdeel en gooit ``ConflictError``.

Deze test pint twee dingen vast. De uitleg uit de fout bereikt de gebruiker via dezelfde
weg als een validatiefout (de stap met ``global_errors``), en hij gaat onbewerkt door:
de aanhef van een validatiefout ("want het project is daarna niet geldig") is onwaar voor
een conflict en zou een tweede oorzaak in dezelfde zin zetten.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.services.project_store import ConflictError
from opi.web.router_approvals import _do_submit

CONFLICT_TEKST = "Project 'demo' is gewijzigd sinds je begon met bewerken"


def _project_manager(fout: Exception) -> MagicMock:
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value={"name": "demo", "domains": []})
    manager.save_and_commit_project = AsyncMock(side_effect=fout)
    manager.close = AsyncMock()
    return manager


async def _besluit_met(fout: Exception) -> tuple[Any, list[str]]:
    """Dien een besluit in met een falende save; geef (antwoord, global_errors) terug."""
    sectie = MagicMock()
    sectie.post_merge = None
    flow = MagicMock()
    flow.sections = [sectie]

    state = MagicMock()
    state.get_merged_data.return_value = {}

    opgevangen: list[str] = []

    def _stap(*args: Any, **kwargs: Any) -> str:
        opgevangen.extend(kwargs.get("global_errors") or [])
        return "stap"

    with (
        patch("opi.web.router_approvals.load_modal_state_for", return_value=state),
        patch("opi.web.router_approvals.get_flow", return_value=flow),
        patch("opi.web.router_approvals.get_project_store", return_value=MagicMock()),
        patch("opi.manager.project_manager.ProjectManager", return_value=_project_manager(fout)),
        patch("opi.web.router_approvals._render_section_html", return_value="sectie"),
        patch("opi.web.router_approvals._render_modal_step", side_effect=_stap),
    ):
        antwoord = await _do_submit(MagicMock(), "token", {"email": "beheerder@example.com"}, "demo")
    return antwoord, opgevangen


@pytest.mark.asyncio
async def test_conflict_komt_onbewerkt_terug_en_geen_500() -> None:
    """Een ConflictError uit de store komt als melding terug, met zijn eigen tekst."""
    antwoord, global_errors = await _besluit_met(ConflictError(CONFLICT_TEKST))

    assert antwoord.body.decode() == "stap", "een botsing moet de stap met een melding opleveren"
    assert global_errors == [CONFLICT_TEKST]


@pytest.mark.asyncio
async def test_validatiefout_houdt_zijn_aanhef() -> None:
    """De bestaande weg voor een validatiefout verandert niet door de toevoeging."""
    antwoord, global_errors = await _besluit_met(ProjectIntegrityError("subdomein 'web' bestaat niet"))

    assert antwoord.body.decode() == "stap"
    assert global_errors == [
        "Het besluit is niet opgeslagen, want het project is daarna niet geldig: subdomein 'web' bestaat niet"
    ]


@pytest.mark.asyncio
async def test_een_onbekende_fout_wordt_niet_ingeslikt() -> None:
    """Alleen deze drie soorten worden opgevangen; de rest hoort door te vallen."""
    with pytest.raises(RuntimeError):
        await _besluit_met(RuntimeError("iets heel anders"))
