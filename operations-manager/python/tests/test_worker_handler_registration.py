"""The in-process worker must register a handler for every task type it can be handed.

The deployed pod runs the worker in-process, started in the API server's lifespan
(``opi/server.py``). Any ``TaskType`` an endpoint enqueues is picked up there, so a
task type present in the enum but missing from that registration is enqueued fine
(202) and then fails at run time with "No handler registered for task type: ...".

That is exactly how the unified per-service config endpoint (``CONFIGURE_SERVICE``)
was broken: the handler was wired into the standalone ``opi/worker_main.py`` entry
point only, not into ``server.py``, so the endpoint accepted the request and the
in-process worker then rejected the task. This guard parses ``server.py`` and fails
loudly if any ``TaskType`` member has no in-process handler, so a new task type
cannot ship half-wired.
"""

from __future__ import annotations

import ast
from pathlib import Path

from opi.core.async_task_service import TaskType

_SERVER = Path(__file__).resolve().parent.parent / "opi" / "server.py"


def _registered_task_types(source_path: Path) -> set[str]:
    """Collect the ``TaskType.<NAME>`` first argument of every ``register_handler`` call."""
    tree = ast.parse(source_path.read_text())
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "register_handler"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name) and first.value.id == "TaskType":
            registered.add(first.attr)
    return registered


def test_in_process_worker_registers_every_task_type() -> None:
    registered = _registered_task_types(_SERVER)
    all_types = {member.name for member in TaskType}

    assert registered, "no register_handler(TaskType.*) calls found in server.py"
    missing = all_types - registered
    assert not missing, (
        "the in-process worker (server.py lifespan) has no handler for these task types, "
        f"so an endpoint enqueuing one fails at run time: {sorted(missing)}"
    )
