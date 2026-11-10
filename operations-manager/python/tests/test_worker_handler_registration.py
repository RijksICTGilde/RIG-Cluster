"""The worker must have a handler for every task type it can be handed.

The deployed pod runs the worker in-process, started in the API server's lifespan
(``opi/server.py``). Any ``TaskType`` an endpoint enqueues is picked up there, so a
task type present in the enum but missing from the registration is enqueued fine
(202) and then fails at run time with "No handler registered for task type: ...".

That is exactly how the unified per-service config endpoint (``CONFIGURE_SERVICE``)
was broken: the handler was wired into the standalone ``opi/worker_main.py`` entry
point only, not into ``server.py``, so the endpoint accepted the request and the
in-process worker then rejected the task. Both entry points read one list since
RC-472, and this guard fails loudly if any ``TaskType`` member is missing from it,
so a new task type cannot ship half-wired.
"""

from __future__ import annotations

from opi.runtime.tasks.handler_registry import task_handlers
from opi.services.tasks.async_task_service import TaskType


def test_in_process_worker_registers_every_task_type() -> None:
    registered = task_handlers()
    all_types = set(TaskType)

    assert registered, "the handler registry is empty"
    missing = all_types - registered.keys()
    assert not missing, (
        "the in-process worker (server.py lifespan) has no handler for these task types, "
        f"so an endpoint enqueuing one fails at run time: {sorted(member.name for member in missing)}"
    )
