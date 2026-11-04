"""Tests voor de wekker van de taakworker.

Bij leegloop wachtte de worker een vast pollinterval van twee seconden, ook als de taak in
hetzelfde proces werd aangemaakt: gemiddeld een seconde waarin niets gebeurde, bovenop elke
taak (RC-418). Er is nu een wekker, en het pollen blijft het vangnet voor een taak die een
ander proces aanmaakt.
"""

import asyncio
import time

import pytest
from opi.core.task_worker import TaskWorker, notify_new_task


def _worker(monkeypatch: pytest.MonkeyPatch, poll: float) -> TaskWorker:
    """Een verse worker, dus ook een verse wekker op de loop van deze test."""
    monkeypatch.setattr("opi.core.task_worker.settings.TASK_WORKER_POLL_INTERVAL", poll)
    return TaskWorker(task_service=None, cluster="c1")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_de_wekker_maakt_de_wacht_meteen_af(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _worker(monkeypatch, poll=30.0)

    async def wek_straks() -> None:
        await asyncio.sleep(0.02)
        notify_new_task()

    begonnen = time.monotonic()
    await asyncio.gather(worker._wait_for_work(), wek_straks())

    assert time.monotonic() - begonnen < 5.0, "de wacht liep door tot het pollinterval"


@pytest.mark.asyncio
async def test_zonder_wekker_wacht_hij_het_pollinterval_uit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Het vangnet: een taak uit een ander proces zet deze wekker niet."""
    worker = _worker(monkeypatch, poll=0.05)

    begonnen = time.monotonic()
    await worker._wait_for_work()

    assert time.monotonic() - begonnen >= 0.05


@pytest.mark.asyncio
async def test_een_wekker_die_al_afgegaan_is_laat_de_wacht_niet_beginnen(monkeypatch: pytest.MonkeyPatch) -> None:
    """De lus wist de wekker voor de claim, dus een gezette wekker hoort meteen door te gaan."""
    worker = _worker(monkeypatch, poll=30.0)
    notify_new_task()

    begonnen = time.monotonic()
    await worker._wait_for_work()

    assert time.monotonic() - begonnen < 5.0


def test_de_lus_wist_de_wekker_voor_hij_claimt() -> None:
    """Wissen NA de claim zou een taak missen die er tussen de claim en de wacht bij kwam."""
    import ast
    import inspect

    bron = inspect.getsource(TaskWorker._main_loop)
    module = ast.parse(bron.strip())
    regels = [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in ("clear", "claim_next_task"))
            or (isinstance(node.func, ast.Name) and node.func.id == "clear")
        )
    ]
    wissen = min(
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "clear"
    )
    claimen = min(
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "claim_next_task"
    )
    assert regels, "geen clear() en claim_next_task() in de lus gevonden"
    assert wissen < claimen, "de wekker moet gewist worden voordat er geclaimd wordt"
