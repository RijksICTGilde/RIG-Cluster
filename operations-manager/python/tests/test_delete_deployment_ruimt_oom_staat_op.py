"""Een verwijderd deployment laat geen OOM-tunestaat achter.

``_oom_tune_attempts`` en ``_last_tuned_pod_template_hash`` zijn moduledicts die alleen
door een expliciete reset geleegd worden, en het delete-pad riep die niet aan. Een
verwijderd deployment liet zijn twee sporen dus staan tot het proces herstartte.

Weeg dit op zijn werkelijke gewicht: het is netheid, geen lek. Nagemeten kost het 256
bytes per verweesd deployment (250 KB per duizend), en verkeerd gedrag levert het niet
op, want een deployment dat terugkomt loopt langs ``handle_create_project`` of
``handle_upsert_deployment`` en die resetten allebei. Vandaar een aanroep van de
bestaande resetfunctie in het delete-pad, en geen opruimmechaniek.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_handlers_deployment import handle_delete_deployment
from opi.services import oom_watcher

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _schone_moduledicts() -> Iterator[None]:
    """Moduletoestand maakt tests anders volgordeafhankelijk; leeg voor en na."""
    oom_watcher._oom_tune_attempts.clear()
    oom_watcher._last_tuned_pod_template_hash.clear()
    yield
    oom_watcher._oom_tune_attempts.clear()
    oom_watcher._last_tuned_pod_template_hash.clear()


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task = MagicMock(return_value="task-handle")
    return progress


def _pm() -> MagicMock:
    pm = MagicMock()
    pm.delete_deployment = AsyncMock(
        return_value={"success": True, "errors": [], "operations": [], "already_absent": False}
    )
    pm.close = AsyncMock()
    return pm


async def _verwijder(project: str, deployment: str) -> None:
    with patch("opi.manager.project_manager.create_project_manager", return_value=_pm()):
        await handle_delete_deployment({"project_name": project, "deployment_name": deployment}, _progress())


@pytest.mark.asyncio
async def test_verwijderen_ruimt_beide_sporen_van_de_tune_op() -> None:
    """Aanmaken, laten tunen, verwijderen: geen van beide dicts houdt nog een entry vast."""
    # Laat tunen langs de echte registratiewegen, niet door de dicts zelf te vullen.
    oom_watcher._record_oom_tune_attempt("demo", "pr-1")
    oom_watcher._record_oom_tune_hash("demo", "pr-1", "pr-1-api", "fb654fcc5")
    assert oom_watcher._oom_tune_attempts.get("demo/pr-1") == 1, "voorwaarde: er staat tunestaat"
    assert oom_watcher._last_tuned_pod_template_hash.get("demo/pr-1/pr-1-api") == "fb654fcc5"

    await _verwijder("demo", "pr-1")

    assert "demo/pr-1" not in oom_watcher._oom_tune_attempts, (
        "de pogingenteller van een verwijderd deployment bleef staan"
    )
    assert not [k for k in oom_watcher._last_tuned_pod_template_hash if k.startswith("demo/pr-1/")], (
        "de pod-template-hash van een verwijderd deployment bleef staan"
    )


@pytest.mark.asyncio
async def test_het_opruimen_blijft_bij_het_verwijderde_deployment() -> None:
    """Negatieve controle: een buurdeployment in hetzelfde project houdt zijn budget.

    Zonder deze controle bewijst de test hierboven ook door als de reset alles leegt --
    en dan zou het verwijderen van een preview de rem van elk ander deployment in het
    project opheffen.
    """
    oom_watcher._record_oom_tune_attempt("demo", "pr-1")
    oom_watcher._record_oom_tune_hash("demo", "pr-1", "pr-1-api", "aaa")
    oom_watcher._record_oom_tune_attempt("demo", "pr-2")
    oom_watcher._record_oom_tune_hash("demo", "pr-2", "pr-2-api", "bbb")

    await _verwijder("demo", "pr-1")

    assert oom_watcher._oom_tune_attempts.get("demo/pr-2") == 1, "het buurdeployment verloor zijn pogingenteller"
    assert oom_watcher._last_tuned_pod_template_hash.get("demo/pr-2/pr-2-api") == "bbb", (
        "het buurdeployment verloor zijn generatiegrendel"
    )
