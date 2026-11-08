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
WORKER = PYTHON_ROOT / "opi" / "worker_main.py"

#: De TaskTypes die ``opi/worker_main.py`` met opzet niet registreert. Die losse worker
#: wordt vandaag niet uitgerold (``infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml``
#: laat zien dat de taken in het OPI-proces lopen), en backup en restore vragen meer dan een
#: registratie: ze hebben kopia in de image en de gelijktijdigheidsgrens van
#: ``BACKUP_MAX_CONCURRENT`` nodig. Een regel hier is dus een BESLISSING: wie ze daar
#: registreert, haalt ze hier weg en laat deze toets meekijken.
WORKER_WITHOUT_HANDLER = frozenset({"BACKUP", "RESTORE"})

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


def _registered_task_types(path: Path) -> set[str]:
    """De TaskTypes waarvoor dit bestand een handler registreert.

    Uit de AST en niet uit een import: een registratie staat in de ``lifespan`` van de
    server, en die uitvoeren zou de hele opstelling vragen.
    """
    tree = ast.parse(path.read_text())
    return {
        node.args[0].attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_handler"
        and node.args
        and isinstance(node.args[0], ast.Attribute)
    }


def test_the_server_registers_a_handler_for_every_task_type() -> None:
    """Een TaskType zonder handler komt in de rij en blijft daar liggen.

    Dat is geen fout die een toets op de handler zelf vangt: de taak wordt aangenomen, de
    aanroeper krijgt een taak-id, en er gebeurt nooit iets.
    """
    missing = sorted({task.name for task in TaskType} - _registered_task_types(SERVER))
    assert not missing, (
        f"TaskTypes zonder register_handler in opi/server.py: {missing}. Een taak van dat "
        "type komt in de rij en wordt nooit uitgevoerd."
    )


def test_the_standalone_worker_registers_what_it_claims_to() -> None:
    """Dezelfde eis voor de losse worker, op zijn eigen uitzonderingslijst.

    De lijst hoort te krimpen en niet te groeien: staat een type er wel, dan hoort het uit
    ``WORKER_WITHOUT_HANDLER``.
    """
    registered = _registered_task_types(WORKER)
    expected = {task.name for task in TaskType} - WORKER_WITHOUT_HANDLER

    missing = sorted(expected - registered)
    assert not missing, f"TaskTypes zonder register_handler in opi/worker_main.py: {missing}"

    gone = sorted(WORKER_WITHOUT_HANDLER & registered)
    assert not gone, f"opi/worker_main.py registreert {gone} wel; haal ze uit WORKER_WITHOUT_HANDLER in deze toets."


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


def test_a_finished_phase_carries_no_planned_rows() -> None:
    """Een fase met ``gedaan:`` komt niet terug, dus een planned regel ernaar meet nooit iets.

    Dat is de stille fout die de kaart te somber of te rooskleurig laat staan: de fase is af,
    de regel blijft wachten op een ronde die niet meer volgt.
    """
    finished = {phase for phase, description in _map()["phases"].items() if description.startswith("gedaan:")}
    waiting = sorted(
        f"{name} -> {dependency}: fase '{entry['phase']}'"
        for name, row in _rows()
        for dependency, entry in (row["dependencies"] or {}).items()
        if entry.get("status") == "planned" and entry.get("phase") in finished
    )
    assert not waiting, (
        f"planned regels die naar een afgeronde fase wijzen: {waiting}. Zet ze op measured met "
        "de toets die ze draait, of op een fase die nog komt, of op exempt met een reden."
    )
