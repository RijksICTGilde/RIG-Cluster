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
    from opi.core.async_task_service import AsyncTaskService, TaskType
    from opi.core.database_pool import DatabasePool
    from opi.core.task_worker import TaskWorker

    # Initialize database pool
    pool = DatabasePool(
        host=settings.DATABASE_HOST,
        user=settings.DATABASE_ADMIN_NAME,
        password=settings.DATABASE_ADMIN_PASSWORD,
        database=settings.DATABASE_NAME,
    )
    await pool.initialize()

    # Create service and worker
    task_service = AsyncTaskService(cluster=settings.CLUSTER_MANAGER)
    worker = TaskWorker(task_service=task_service, cluster=settings.CLUSTER_MANAGER)

    # Register handlers
    from opi.core.task_handlers_components import (
        handle_add_component,
        handle_add_component_to_deployment,
        handle_add_service,
        handle_configure_service,
        handle_update_component,
    )
    from opi.core.task_handlers_deployment import (
        handle_delete_deployment,
        handle_update_image,
    )
    from opi.core.task_handlers_operations import (
        handle_clone_bucket,
        handle_clone_database,
        handle_refresh_deployment,
        handle_refresh_project,
    )
    from opi.core.task_handlers_project import (
        handle_create_project,
        handle_upsert_deployment,
    )
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    worker.register_handler(TaskType.CREATE_PROJECT, handle_create_project)
    worker.register_handler(TaskType.UPSERT_DEPLOYMENT, handle_upsert_deployment)
    worker.register_handler(TaskType.UPDATE_IMAGE, handle_update_image)
    worker.register_handler(TaskType.DELETE_DEPLOYMENT, handle_delete_deployment)
    worker.register_handler(TaskType.CLONE_DATABASE, handle_clone_database)
    worker.register_handler(TaskType.CLONE_BUCKET, handle_clone_bucket)
    worker.register_handler(TaskType.REFRESH_DEPLOYMENT, handle_refresh_deployment)
    worker.register_handler(TaskType.SLEEP_DEPLOYMENT, handle_sleep_transition)
    worker.register_handler(TaskType.WAKE_DEPLOYMENT, handle_sleep_transition)
    worker.register_handler(TaskType.REFRESH_PROJECT, handle_refresh_project)
    worker.register_handler(TaskType.ADD_COMPONENT, handle_add_component)
    worker.register_handler(TaskType.UPDATE_COMPONENT, handle_update_component)
    worker.register_handler(TaskType.ADD_COMPONENT_TO_DEPLOYMENT, handle_add_component_to_deployment)
    worker.register_handler(TaskType.ADD_SERVICE, handle_add_service)
    worker.register_handler(TaskType.CONFIGURE_SERVICE, handle_configure_service)

    # Handle graceful shutdown
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
