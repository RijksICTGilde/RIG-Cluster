"""A finished task may not leave state behind in the worker process.

The standalone worker (``opi/worker_main.py``) runs no sweeper, so anything a task adds
to a module-level dict stays there for the life of the process and grows per task. The
check is deliberately blind to which module holds it: it compares the size of every
module-level container under ``opi.core`` around one task.
"""

import sys
from unittest.mock import AsyncMock, patch

import pytest
from opi.runtime.tasks.task_worker import TaskWorker


def _container_sizes() -> dict[str, int]:
    """Per module-level dict, list and set under ``opi.core``, its current size."""
    sizes: dict[str, int] = {}
    for name, module in list(sys.modules.items()):
        if not name.startswith("opi.core."):
            continue
        for attribute, value in list(vars(module).items()):
            if attribute.startswith("__"):
                continue
            if isinstance(value, dict | list | set):
                sizes[f"{name}.{attribute}"] = len(value)
    return sizes


@pytest.fixture
def worker() -> TaskWorker:
    service = AsyncMock()
    service.find_conflicting_task = AsyncMock(return_value=None)
    with patch("opi.runtime.tasks.task_worker.settings") as settings:
        settings.TASK_WORKER_CONCURRENCY = 1
        settings.TASK_WORKER_HEARTBEAT_INTERVAL = 0.05
        settings.TASK_WORKER_MAX_DURATION = 30
        settings.TASK_WORKER_MAX_ATTEMPTS = 3
        yield TaskWorker(task_service=service, cluster="test-cluster")


async def test_a_finished_task_leaves_no_module_level_state(worker: TaskWorker) -> None:
    worker.register_handler("update_image", AsyncMock(return_value={"status": "ok"}))
    task = {
        "task_id": "task-1",
        "task_type": "update_image",
        "project_name": "demo",
        "payload": {},
        "attempt_count": 0,
        "max_attempts": 3,
        "created_by": None,
    }

    before = _container_sizes()
    await worker._execute_task(task)
    after = _container_sizes()

    grown = {key: (before.get(key, 0), size) for key, size in after.items() if size > before.get(key, 0)}
    assert not grown, f"the task left state behind: {grown}"
