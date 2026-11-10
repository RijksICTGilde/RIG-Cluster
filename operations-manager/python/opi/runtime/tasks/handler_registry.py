"""Which handler runs which task type, in one list for both worker entry points.

The server runs the worker in-process (``opi/server.py``); ``opi/worker_main.py`` is the
standalone process. Registering by hand in both let a handler reach one and not the other,
which is how ``CONFIGURE_SERVICE`` once shipped half-wired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.services.tasks.async_task_service import TaskType

if TYPE_CHECKING:
    from collections.abc import Callable

#: The task types the standalone worker deliberately leaves out. Backup and restore need
#: more than a registration there: kopia in the image and the ``BACKUP_MAX_CONCURRENT``
#: limit the server sets. Taking one off this set is a decision, and
#: ``tests/test_actions_map.py`` keeps its own copy so it has to be made twice.
COMBINED_MODE_ONLY: frozenset[TaskType] = frozenset({TaskType.BACKUP, TaskType.RESTORE})


def task_handlers() -> dict[TaskType, Callable[..., Any]]:
    """Every task type with its handler.

    The imports are local because the handler modules reach back into the managers, and
    the registration itself runs inside the server's lifespan.
    """
    from opi.runtime.backup.task_handlers_backup import handle_backup, handle_restore
    from opi.runtime.tasks.task_handlers_components import (
        handle_add_component,
        handle_add_component_to_deployment,
        handle_add_service,
        handle_configure_service,
        handle_configure_service_values,
        handle_delete_component,
        handle_manage_database_schemas,
        handle_update_component,
    )
    from opi.runtime.tasks.task_handlers_deployment import handle_delete_deployment, handle_update_image
    from opi.runtime.tasks.task_handlers_operations import (
        handle_clone_bucket,
        handle_clone_database,
        handle_refresh_deployment,
        handle_refresh_project,
        handle_rotate_credentials,
        handle_rotate_project_key,
    )
    from opi.runtime.tasks.task_handlers_project import (
        handle_create_project,
        handle_delete_project,
        handle_upsert_deployment,
    )
    from opi.services.catalog.attachments.task import handle_configure_attachment, handle_delete_attachment
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    return {
        TaskType.CREATE_PROJECT: handle_create_project,
        TaskType.UPSERT_DEPLOYMENT: handle_upsert_deployment,
        TaskType.UPDATE_IMAGE: handle_update_image,
        TaskType.DELETE_DEPLOYMENT: handle_delete_deployment,
        TaskType.DELETE_PROJECT: handle_delete_project,
        TaskType.DELETE_COMPONENT: handle_delete_component,
        TaskType.DELETE_ATTACHMENT: handle_delete_attachment,
        TaskType.CONFIGURE_ATTACHMENT: handle_configure_attachment,
        TaskType.CLONE_DATABASE: handle_clone_database,
        TaskType.CLONE_BUCKET: handle_clone_bucket,
        TaskType.REFRESH_DEPLOYMENT: handle_refresh_deployment,
        TaskType.ROTATE_CREDENTIALS: handle_rotate_credentials,
        TaskType.SLEEP_DEPLOYMENT: handle_sleep_transition,
        TaskType.WAKE_DEPLOYMENT: handle_sleep_transition,
        TaskType.REFRESH_PROJECT: handle_refresh_project,
        TaskType.ROTATE_PROJECT_KEY: handle_rotate_project_key,
        TaskType.ADD_COMPONENT: handle_add_component,
        TaskType.UPDATE_COMPONENT: handle_update_component,
        TaskType.ADD_COMPONENT_TO_DEPLOYMENT: handle_add_component_to_deployment,
        TaskType.ADD_SERVICE: handle_add_service,
        TaskType.CONFIGURE_SERVICE: handle_configure_service,
        TaskType.CONFIGURE_SERVICE_VALUES: handle_configure_service_values,
        TaskType.MANAGE_DATABASE_SCHEMAS: handle_manage_database_schemas,
        TaskType.BACKUP: handle_backup,
        TaskType.RESTORE: handle_restore,
    }


def standalone_worker_handlers() -> dict[TaskType, Callable[..., Any]]:
    """What ``opi/worker_main.py`` registers: everything that does not need combined mode."""
    return {task_type: handler for task_type, handler in task_handlers().items() if task_type not in COMBINED_MODE_ONLY}
