"""Standalone task worker process.

Run with: python -m opi.worker_main

This process claims and executes tasks from the async_tasks table.
It does not serve HTTP. Deploy alongside the frontend OPI for scaling.
"""

import asyncio
import logging
import signal

from opi.core.config import settings

logger = logging.getLogger(__name__)


async def main() -> None:
    """Initialize and run the standalone task worker."""
    from opi.core.database_pool import DatabasePool
    from opi.runtime.tasks.task_worker import TaskWorker
    from opi.services.tasks.async_task_service import AsyncTaskService

    # The app role, like the frontend's main pool: the worker only touches OPI's own tables.
    pool = DatabasePool(
        host=settings.DATABASE_HOST,
        user=settings.DATABASE_APP_USER,
        password=settings.database_app_password,
        database=settings.DATABASE_NAME,
        sslmode=settings.DATABASE_SSLMODE,
    )
    await pool.initialize()

    task_service = AsyncTaskService(cluster=settings.CLUSTER_MANAGER)
    worker = TaskWorker(task_service=task_service, cluster=settings.CLUSTER_MANAGER)

    from opi.runtime.tasks.handler_registry import standalone_worker_handlers

    for task_type, handler in standalone_worker_handlers().items():
        worker.register_handler(task_type, handler)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    logger.info("Standalone task worker starting for cluster: %s", settings.CLUSTER_MANAGER)

    try:
        await worker.run()
    finally:
        await worker.stop()
        await pool.close()
        logger.info("Standalone task worker shut down")


if __name__ == "__main__":
    asyncio.run(main())
