"""De kaart van alle acties blijft volledig en waar.

``tests/regression/actions.yaml`` noemt elke stap van het opstarten en elke taak, met per
afhankelijkheid of hij tegen de echte afhankelijkheid gemeten is. Deze toets eist dat de
kaart klopt met de code: een nieuwe stap in ``lifespan`` of ``run_startup_tasks``, of een
nieuw ``TaskType``, laat hem falen tot er een regel bij staat. Zo kan de kaart niet stil
verouderen, en is in een bestand te zien wat nog ``planned`` is.

Statisch, dus hij draait in de standaardrun; de groepen zelf hebben hun eigen task.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path
from typing import Any

import pytest
import yaml
from opi.core.async_task_service import TaskType

PYTHON_ROOT = Path(__file__).resolve().parent.parent
MAP = PYTHON_ROOT / "tests" / "regression" / "actions.yaml"
SERVER = PYTHON_ROOT / "opi" / "server.py"
STARTUP = PYTHON_ROOT / "opi" / "core" / "startup.py"

#: Waar een toets mag staan die de kaart als ``measured`` noemt.
TEST_ROOTS = (PYTHON_ROOT / "tests",)

STATUSES = ("measured", "planned", "exempt")


def _map() -> dict[str, Any]:
    return yaml.safe_load(MAP.read_text())


def _called_names(path: Path, function: str) -> set[str]:
    """De functies en klassen die ``function`` bij naam aanroept.

    Dat is wat een stap van het opstarten is: een aanroep in ``lifespan`` of in
    ``run_startup_tasks``. Ingebouwde functies blijven buiten de kaart.
    """
    tree = ast.parse(path.read_text())
    body = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
    )
    return {
        node.func.id
        for node in ast.walk(body)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not hasattr(builtins, node.func.id)
    }


def _test_names() -> set[str]:
    """Elke toetsnaam in ``tests/``, zodat een verwijzing in de kaart te controleren is."""
    names: set[str] = set()
    for root in TEST_ROOTS:
        for path in root.rglob("test_*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
                    names.add(node.name)
    return names


def _rows() -> list[tuple[str, dict[str, Any]]]:
    document = _map()
    rows = [(f"startup:{row['step']}", row) for row in document["startup"]]
    rows += [(f"task:{row['task']}", row) for row in document["tasks"]]
    rows += [(f"test_setup:{row['subject']}", row) for row in document["test_setup"]]
    return rows


def test_every_startup_step_is_on_the_map() -> None:
    on_the_map = {row["step"] for row in _map()["startup"]}
    in_the_code = _called_names(SERVER, "lifespan") | _called_names(STARTUP, "run_startup_tasks")

    missing = sorted(in_the_code - on_the_map)
    assert not missing, (
        f"Stappen van het opstarten zonder regel op de kaart: {missing}. Zet ze in "
        "tests/regression/actions.yaml, met per afhankelijkheid measured, planned of exempt."
    )

    gone = sorted(on_the_map - in_the_code)
    assert not gone, f"De kaart noemt stappen die het opstarten niet meer doet: {gone}"


def test_every_task_type_is_on_the_map() -> None:
    on_the_map = {row["task"] for row in _map()["tasks"]}
    in_the_code = {task.name for task in TaskType}

    missing = sorted(in_the_code - on_the_map)
    assert not missing, f"TaskTypes zonder regel op de kaart: {missing}"

    gone = sorted(on_the_map - in_the_code)
    assert not gone, f"De kaart noemt TaskTypes die niet bestaan: {gone}"


@pytest.mark.parametrize(("name", "row"), _rows(), ids=lambda value: value if isinstance(value, str) else "")
def test_every_dependency_has_a_usable_status(name: str, row: dict[str, Any]) -> None:
    phases = set(_map()["phases"])
    for dependency, entry in (row["dependencies"] or {}).items():
        where = f"{name} -> {dependency}"
        status = entry.get("status")
        assert status in STATUSES, f"{where}: status '{status}' is geen van {STATUSES}"
        if status == "measured":
            assert entry.get("test"), f"{where}: measured zonder de toets die hem draait"
        elif status == "planned":
            assert entry.get("phase") in phases, (
                f"{where}: planned met fase '{entry.get('phase')}', niet een van {sorted(phases)}"
            )
        else:
            assert entry.get("reason"), f"{where}: exempt zonder reden"


def test_every_named_test_exists() -> None:
    """Een kaart die naar een toets wijst die niet bestaat, meet niets."""
    available = _test_names()
    missing = sorted(
        f"{name} -> {dependency}: {entry['test']}"
        for name, row in _rows()
        for dependency, entry in (row["dependencies"] or {}).items()
        if entry.get("status") == "measured" and entry["test"] not in available
    )
    assert not missing, f"De kaart noemt toetsen die niet bestaan: {missing}"
