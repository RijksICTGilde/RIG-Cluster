"""Een taak waarvan het werk mislukte, meldt dat in zijn eigen ``status``.

Waar dit vandaan komt: de generale repetitie
(``docs/generale-repetitie-2026-08-12.md``, bevinding 5) mat op een afgewezen
dienstselectie taak ``0d504be5``::

    "status": "completed", "progress_percent": 100,
    "subtasks": [ ... {"name": "Component toevoegen", "status": "failed", ...} ],
    "result": {"status": "failed", "error_type": "invalid_services"},
    "error_message": "Services that must be enabled at project level first: ..."

De fout stond er dus wel - in ``result``, in ``error_message`` en op de subtaak - maar
niet in het veld waar een aanroeper als EERSTE op kijkt. Wie op ``status`` polt, en dat
doet de zad-cli, zag een afgewezen wijziging aan voor een geslaagde.

Het weigeren zelf is goed en blijft; het rapporteren was fout. Deze tests bewaken de drie
manieren waarop een handler kan falen en de eis dat een GESLAAGDE taak zich niet anders
gaat gedragen.

Run: uv run pytest tests/test_task_status_reports_failure.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_worker import TaskWorker, reported_failure

# --------------------------------------------------------------------------------------
# De beslissing zelf
# --------------------------------------------------------------------------------------


class _Voortgang:
    """Zo veel van de voortgangsmanager als deze beslissing leest."""

    def __init__(self, project_failure: str | None = None) -> None:
        self.project_failure = project_failure


def test_status_failed_is_een_mislukking() -> None:
    """De vorm uit de meting: de component- en dienstenhandlers geven dit terug."""
    resultaat = {
        "status": "failed",
        "error": "Services that must be enabled at project level first: ['publish-on-web']",
        "error_type": "invalid_services",
    }
    assert reported_failure(resultaat, _Voortgang()) == resultaat["error"]


def test_success_false_is_een_mislukking() -> None:
    """De andere vorm, die al gelezen werd: de backup- en herstelhandlers."""
    assert reported_failure({"success": False, "error": "kapot"}, _Voortgang()) == "kapot"


def test_een_gefaald_project_telt_ook_als_de_handler_zwijgt() -> None:
    """De derde weg: ``fail_project()`` aanroepen en toch iets succesvols teruggeven.

    Dat markeerde de taak wel als mislukt, maar fire-and-forget - waarna de worker er
    ``completed`` overheen schreef. Wie het laatst schrijft wint, en dat was de fout niet.
    """
    assert reported_failure({"success": True}, _Voortgang("Herstel gedeeltelijk mislukt")) == (
        "Herstel gedeeltelijk mislukt"
    )


def test_een_geslaagde_taak_blijft_geslaagd() -> None:
    """De andere helft van de poort: geen enkel bestaand geslaagd antwoord verandert."""
    assert reported_failure({"status": "success", "message": "klaar"}, _Voortgang()) is None
    assert reported_failure({"success": True}, _Voortgang()) is None
    assert reported_failure({"status": "completed", "deleted": True}, _Voortgang()) is None
    assert reported_failure({"status": "superseded", "message": "overgenomen"}, _Voortgang()) is None
    assert reported_failure(None, _Voortgang()) is None


def test_een_mislukking_zonder_tekst_krijgt_toch_een_melding() -> None:
    """``error_message`` mag niet leeg blijven; dan staat er niets in het portaal."""
    melding = reported_failure({"status": "failed"}, _Voortgang())
    assert melding, "een mislukte taak zonder fouttekst hoort alsnog iets te melden"


# --------------------------------------------------------------------------------------
# En wat de worker er dan mee doet
# --------------------------------------------------------------------------------------


def _taak() -> dict[str, Any]:
    return {
        "task_id": "test-id",
        "task_type": "add_component",
        "project_name": "test",
        "payload": {},
        "attempt_count": 0,
        "max_attempts": 3,
    }


@pytest.fixture
def task_service() -> AsyncMock:
    service = AsyncMock()
    service.claim_next_task = AsyncMock(return_value=None)
    service.start_task = AsyncMock()
    service.send_heartbeat = AsyncMock()
    service.complete_task = AsyncMock()
    service.fail_task = AsyncMock()
    service.recover_stale_tasks = AsyncMock(return_value=0)
    service.cleanup_old_tasks = AsyncMock(return_value=0)
    service.update_progress = AsyncMock()
    service.find_conflicting_task = AsyncMock(return_value=None)
    return service


@pytest.fixture
def worker_settings() -> Any:
    with patch("opi.core.task_worker.settings") as s:
        s.TASK_WORKER_POLL_INTERVAL = 0.05
        s.TASK_WORKER_HEARTBEAT_INTERVAL = 0.05
        s.TASK_WORKER_STALE_THRESHOLD = 120
        s.TASK_WORKER_MAX_ATTEMPTS = 3
        s.TASK_WORKER_CLEANUP_RETENTION_HOURS = 72
        s.TASK_WORKER_CONCURRENCY = 1
        s.TASK_WORKER_MAX_DURATION = 1800
        yield s


async def _draai_tot(worker: TaskWorker, klaar: asyncio.Event, timeout: float = 2.0) -> None:
    run_task = asyncio.create_task(worker.run())
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(klaar.wait(), timeout=timeout)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task


@patch("opi.core.persistent_task_progress.PersistentTaskProgressManager")
async def test_de_afgewezen_dienstselectie_meldt_zich_als_mislukt(
    mock_progress_cls: MagicMock,
    task_service: AsyncMock,
    worker_settings: Any,
) -> None:
    """Het geval uit de meting, van handler tot taakrecord.

    De poort is niet alleen dat ``fail_task`` gebeld wordt, maar ook dat het antwoord van
    de handler bewaard blijft: daar zit ``error_type``, en daar stuurt de API-laag op.
    """
    del worker_settings
    worker = TaskWorker(task_service=task_service, cluster="test-cluster")
    task_service.claim_next_task.side_effect = [_taak(), None]

    voortgang = AsyncMock()
    voortgang.mark_legacy_completed = MagicMock()
    voortgang.mark_legacy_failed = MagicMock()
    voortgang.project_failure = None
    mock_progress_cls.return_value = voortgang

    antwoord = {
        "component_name": "web",
        "status": "failed",
        "error": "Services that must be enabled at project level first: ['publish-on-web']",
        "error_type": "invalid_services",
    }
    worker.register_handler("add_component", AsyncMock(return_value=antwoord))

    klaar = asyncio.Event()
    origineel = task_service.fail_task

    async def _signaleer(*args: Any, **kwargs: Any) -> None:
        await origineel(*args, **kwargs)
        klaar.set()

    task_service.fail_task = AsyncMock(side_effect=_signaleer)

    await _draai_tot(worker, klaar)

    task_service.complete_task.assert_not_called()
    task_service.fail_task.assert_called_once()
    kwargs = task_service.fail_task.call_args.kwargs
    assert kwargs["task_id"] == "test-id"
    assert "publish-on-web" in kwargs["error_message"]
    assert kwargs["max_attempts"] == 0, "een afgewezen wijziging hoort niet opnieuw geprobeerd te worden"
    assert kwargs["result"] == antwoord, "het antwoord van de handler hoort bewaard te blijven"
    voortgang.mark_legacy_failed.assert_called_once()


@patch("opi.core.persistent_task_progress.PersistentTaskProgressManager")
async def test_een_geslaagde_taak_verandert_niet_van_gedrag(
    mock_progress_cls: MagicMock,
    task_service: AsyncMock,
    worker_settings: Any,
) -> None:
    """De regressiepoort: wat slaagde, slaagt nog steeds en op dezelfde manier."""
    del worker_settings
    worker = TaskWorker(task_service=task_service, cluster="test-cluster")
    task_service.claim_next_task.side_effect = [_taak(), None]

    voortgang = AsyncMock()
    voortgang.mark_legacy_completed = MagicMock()
    voortgang.mark_legacy_failed = MagicMock()
    voortgang.project_failure = None
    mock_progress_cls.return_value = voortgang

    antwoord = {"component_name": "web", "status": "success", "message": "Component 'web' added successfully"}
    worker.register_handler("add_component", AsyncMock(return_value=antwoord))

    klaar = asyncio.Event()
    origineel = task_service.complete_task

    async def _signaleer(*args: Any, **kwargs: Any) -> None:
        await origineel(*args, **kwargs)
        klaar.set()

    task_service.complete_task = AsyncMock(side_effect=_signaleer)

    await _draai_tot(worker, klaar)

    task_service.complete_task.assert_called_once_with("test-id", antwoord)
    task_service.fail_task.assert_not_called()
    voortgang.mark_legacy_completed.assert_called_once()
