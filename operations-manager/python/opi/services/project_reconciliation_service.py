"""Record what a processing run actually reconciled (RC-188).

One writer, ``ProjectManager.process_project``; the reader is the drift count in
``AsyncTaskService.get_deferred_rollouts``.
"""

from __future__ import annotations

from sqlalchemy import func, literal_column
from sqlalchemy.dialects.postgresql import insert

from opi.core.db import session_scope
from opi.services.persistence.project_reconciliation import SCOPE_KEY_SQL, ProjectReconciliation


async def record_reconciliation(
    project_name: str,
    deployment_names: list[str],
    *,
    project_wide: bool,
    read_seconds_ago: float,
) -> None:
    """Upsert the reconciliation rows of one run.

    The stored moment is when the run READ the project file, not when it finished: a
    change saved while it ran is not in its snapshot (RC-82). It is computed on the
    database clock, the clock ``async_tasks.completed_at`` is compared against, by
    subtracting how long ago the read was.

    GREATEST, because two runs may overlap and the one that read earlier can finish
    last; it must not move a scope back in time.
    """
    scopes: list[str | None] = list(deployment_names)
    if project_wide:
        scopes.append(None)
    if not scopes:
        return

    read_at = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, read_seconds_ago)
    statement = insert(ProjectReconciliation).values(
        [{"project_name": project_name, "deployment_name": name, "reconciled_at": read_at} for name in scopes]
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ProjectReconciliation.project_name, literal_column(SCOPE_KEY_SQL)],
        set_={"reconciled_at": func.greatest(ProjectReconciliation.reconciled_at, statement.excluded.reconciled_at)},
    )
    async with session_scope() as session:
        await session.execute(statement)
