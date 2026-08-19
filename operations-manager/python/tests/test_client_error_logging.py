"""Een fout van de aanroeper hoort niet als ERROR in de logs te staan.

De FSC-pipeline vroeg een deployment aan met een verwijzing naar een component dat niet in
de catalogus staat. Dat is een verkeerd verzoek, geen storing van ZAD, maar het kwam twee
keer als ERROR in het centrale log en daarmee in de alertering terecht.
"""

import logging
from unittest.mock import MagicMock, patch

from opi.manager.project_validation import validate_component_references


def _project() -> dict:
    return {"name": "mpfb-8wh", "components": [{"name": "frontend", "type": "single"}]}


def test_onbekende_componentverwijzing_logt_op_warning(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="opi.manager.project_validation"):
        result = validate_component_references(_project(), [{"reference": "logius-fscbootstrap"}], "deployment")

    assert result["success"] is False
    records = [r for r in caplog.records if "Invalid component references" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING]


def _progress():
    """Een echte progress manager zonder zijn periodieke flush-lus."""
    with patch(
        "opi.core.persistent_task_progress.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        from opi.core.persistent_task_progress import PersistentTaskProgressManager

        return PersistentTaskProgressManager(
            task_id="task-under-test",
            project_name="mpfb-8wh",
            task_service=MagicMock(),
        )


def test_fail_task_met_client_error_logt_op_warning(caplog) -> None:
    progress = _progress()
    task_id = progress.add_task("Deployment validatie")

    with caplog.at_level(logging.DEBUG, logger="opi.core.persistent_task_progress"):
        progress.fail_task(task_id, "component bestaat niet", client_error=True)

    records = [r for r in caplog.records if "Failed task" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING]


def test_fail_task_zonder_vlag_blijft_error(caplog) -> None:
    progress = _progress()
    task_id = progress.add_task("Deployment validatie")

    with caplog.at_level(logging.DEBUG, logger="opi.core.persistent_task_progress"):
        progress.fail_task(task_id, "git push mislukt")

    records = [r for r in caplog.records if "Failed task" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.ERROR]
