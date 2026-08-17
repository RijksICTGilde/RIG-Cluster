"""Een handler die GOOIT laat nu ook iets leesbaars achter (zad-cli, punt 26b, laag 3).

Vier taaktypen geven bij een fout geen faal-dict terug maar gooien: ``update_image``,
``delete_deployment`` en de twee clone-taken. De worker bewaarde dan alleen een fouttekst
en helemaal geen ``result``, dus een client kreeg status ``failed`` zonder type en zonder
categorie. Dat is precies wat zij maten op
``deployment update-image productie --component bestaatniet``: een typefout in de aanroep
die bij hen uitkwam als exit 3, "niet toe te schrijven".

Twee dingen worden hier vastgelegd:

1. Een verzoek dat niet kan (``TaskInputError``, of een 4xx uit een manager) is een
   BLIJVENDE mislukking met de reden erbij. Opnieuw proberen maakt een component dat niet
   bestaat niet alsnog waar.
2. Elke andere exceptie laat alsnog een resultaat achter, met ``internal_error``, zodat er
   altijd iets te lezen valt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.task_models import task_response_from_dict
from opi.core.task_errors import TaskInputError
from opi.core.task_worker import TaskWorker

TASK_ID = "11111111-2222-3333-4444-555555555555"


def _task() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "task_type": "update_image",
        "project_name": "p1",
        "deployment_name": "productie",
        "payload": {},
        "attempt_count": 0,
        "max_attempts": 3,
    }


async def _run_with(exception: Exception) -> AsyncMock:
    """Draai één taak waarvan de handler ``exception`` gooit; geef de taakservice terug."""
    task_service = AsyncMock()
    task_service.find_conflicting_task = AsyncMock(return_value=None)
    worker = TaskWorker(task_service=task_service, cluster="local")

    async def handler(payload: dict, progress: Any) -> dict:
        raise exception

    worker.register_handler("update_image", handler)

    # Een voortgangsmanager die niets doet: alleen ``close`` wordt echt afgewacht.
    progress = MagicMock()
    progress.close = AsyncMock()

    with patch("opi.core.persistent_task_progress.PersistentTaskProgressManager", return_value=progress):
        await worker._execute_task(_task())
    return task_service


def _fail_kwargs(task_service: AsyncMock) -> dict[str, Any]:
    assert task_service.fail_task.await_count == 1, "de taak moet precies één keer als gefaald worden gemeld"
    return task_service.fail_task.await_args.kwargs


@pytest.mark.asyncio
class TestEenVerzoekDatNietKan:
    async def test_de_reden_van_de_werper_komt_in_het_resultaat(self) -> None:
        task_service = await _run_with(
            TaskInputError("Component 'bestaatniet' not found in deployment 'productie'", "component_not_found")
        )

        kwargs = _fail_kwargs(task_service)
        assert kwargs["result"]["error_type"] == "component_not_found"
        assert kwargs["result"]["status"] == "failed"
        assert "bestaatniet" in kwargs["result"]["error"]

    async def test_er_volgt_geen_nieuwe_poging(self) -> None:
        """Een tweede poging maakt een component dat niet bestaat niet alsnog waar, en
        kost de wachtende alleen tijd."""
        task_service = await _run_with(TaskInputError("weg", "deployment_not_found"))

        kwargs = _fail_kwargs(task_service)
        assert kwargs["max_attempts"] == 0

    async def test_een_404_uit_een_manager_telt_ook(self) -> None:
        """De managers spreken die taal al; die 404 belandde tot nu toe in de algemene
        tak en dus als 'onbekend' bij de client."""
        task_service = await _run_with(HTTPException(status_code=404, detail="Deployment 'x' not found"))

        kwargs = _fail_kwargs(task_service)
        assert kwargs["result"]["error_type"] == "not_found"
        assert kwargs["result"]["error"] == "Deployment 'x' not found"
        assert kwargs["max_attempts"] == 0

    async def test_een_500_uit_een_manager_blijft_van_ons(self) -> None:
        """Alleen de 4xx-en zijn van de aanroeper. Een 5xx houdt zijn nieuwe pogingen."""
        task_service = await _run_with(HTTPException(status_code=500, detail="upstream weg"))

        kwargs = _fail_kwargs(task_service)
        assert kwargs["result"]["error_type"] == "internal_error"
        assert kwargs["max_attempts"] == 3


@pytest.mark.asyncio
class TestElkeAndereFout:
    async def test_er_valt_altijd_iets_te_lezen(self) -> None:
        task_service = await _run_with(RuntimeError("iets onverwachts"))

        kwargs = _fail_kwargs(task_service)
        assert kwargs["result"]["error_type"] == "internal_error"
        assert "iets onverwachts" in kwargs["result"]["error"]

    async def test_de_pogingen_blijven_zoals_ze_waren(self) -> None:
        task_service = await _run_with(RuntimeError("netwerk hikte"))

        assert _fail_kwargs(task_service)["max_attempts"] == 3

    async def test_de_client_leest_er_een_categorie_uit(self) -> None:
        """De hele keten: wat de worker bewaart komt via het antwoord bij de client, met
        de categorie erbij."""
        task_service = await _run_with(TaskInputError("component weg", "component_not_found"))
        bewaard = _fail_kwargs(task_service)["result"]

        antwoord = task_response_from_dict(
            {
                "task_id": TASK_ID,
                "task_type": "update_image",
                "status": "failed",
                "result": bewaard,
                "created_at": "2026-08-17T00:00:00Z",
            }
        )

        assert antwoord["result"]["error_category"] == "InvalidInput"
