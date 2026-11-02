"""De voortgangsvelden van een taak mogen geen geheim naar de client dragen.

``fail_task`` redigeert ``error_message``, maar dezelfde fouttekst gaat ook als
``subtasks[].error`` naar dezelfde rij, en die velden staan in hetzelfde antwoord. De
redactie zit daarom in ``_flush_to_db``, het enige punt waar die velden de manager
verlaten; de tests meten dus de doorgegeven payload.

``current_step`` draagt de fouttekst sinds RC-282 niet meer (``fail_project`` zet er
"Failed" neer), maar loopt wel langs dezelfde redactie: het veld wordt met vrije tekst
gevuld door ``update_current_step``.
"""

from unittest.mock import AsyncMock

import pytest
from opi.core.persistent_task_progress import PersistentTaskProgressManager
from opi.core.task_manager import _projects

GEHEIM = "geheimwachtwoord"
FOUT = f"kubectl apply mislukt: password={GEHEIM}"


@pytest.fixture(autouse=True)
def clean_projects():
    """Isolate the global legacy _projects dict per test."""
    _projects.clear()
    yield
    _projects.clear()


def _make_progress() -> PersistentTaskProgressManager:
    return PersistentTaskProgressManager(task_id="task-1", project_name="demo", task_service=AsyncMock())


async def _flush(progress: PersistentTaskProgressManager) -> dict:
    await progress._flush_to_db()
    return progress._task_service.update_progress.await_args.kwargs


async def test_fail_project_zet_geen_fouttekst_in_current_step():
    progress = _make_progress()
    progress.fail_project(FOUT)

    doorgegeven = await _flush(progress)
    assert GEHEIM not in doorgegeven["current_step"]


async def test_current_step_met_een_geheim_wordt_gemaskeerd():
    """``update_current_step`` krijgt vrije tekst, dus het veld loopt langs de redactie."""
    progress = _make_progress()
    progress.update_current_step(FOUT)

    doorgegeven = await _flush(progress)
    assert GEHEIM not in doorgegeven["current_step"]
    assert "***REDACTED***" in doorgegeven["current_step"]


async def test_fail_project_maskeert_het_geheim_in_de_legacy_fout():
    progress = _make_progress()
    progress.fail_project(FOUT)

    assert GEHEIM not in str(_projects["task-1"].error)
    assert GEHEIM not in str(progress.project_failure)


async def test_fail_subtask_maskeert_het_geheim():
    progress = _make_progress()
    subtask_id = progress.add_subtask("task-1", "Uitrollen")
    progress.fail_subtask(subtask_id, FOUT)

    doorgegeven = await _flush(progress)
    fout = doorgegeven["subtasks"][0]["error"]
    assert GEHEIM not in fout
    assert "***REDACTED***" in fout


async def test_fail_task_maskeert_het_geheim():
    """De weg met 30+ aanroepers: fail_task schrijft hetzelfde veld als fail_subtask."""
    progress = _make_progress()
    subtask_id = progress.add_subtask("task-1", "Uitrollen")
    progress.fail_task(subtask_id, FOUT)

    doorgegeven = await _flush(progress)
    fout = doorgegeven["subtasks"][0]["error"]
    assert GEHEIM not in fout
    assert "***REDACTED***" in fout


async def test_een_subtask_zonder_fout_houdt_none():
    progress = _make_progress()
    progress.add_subtask("task-1", "Uitrollen")

    doorgegeven = await _flush(progress)
    assert doorgegeven["subtasks"][0]["error"] is None
