"""ORM model for the ``async_tasks`` table (the async task queue, RC-5).

Schema-as-code mirror of ``ASYNC_TASKS_TABLE_SQL``. The repository lives in
``opi/core/async_task_service.py``. Queries still add ``FOR UPDATE SKIP LOCKED`` for
safe concurrent claiming.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, DateTime, Index, SmallInteger, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class AsyncTask(Base):
    """A queued/running async task. Matches ``ASYNC_TASKS_TABLE_SQL`` exactly."""

    __tablename__ = "async_tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    project_name: Mapped[str] = mapped_column(String(63), nullable=False)
    deployment_name: Mapped[str | None] = mapped_column(String(63))
    cluster: Mapped[str] = mapped_column(String(63), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    current_step: Mapped[str | None] = mapped_column(String(255), server_default=text("'Queued'"))
    progress_percent: Mapped[int | None] = mapped_column(SmallInteger, server_default=text("0"))
    subtasks: Mapped[list | None] = mapped_column(JSONB, server_default=text("'[]'"))
    logs: Mapped[list | None] = mapped_column(ARRAY(Text))
    events: Mapped[list | None] = mapped_column(JSONB)
    web_addresses: Mapped[dict | None] = mapped_column(JSONB)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    #: De deployments die deze taak raakt; NULL is projectbreed, net als None in
    #: ``scope_of()``, dat de enige schrijver van deze kolom is.
    #:
    #: De postgresql-ARRAY en niet de generieke uit ``sqlalchemy`` (die ``logs`` gebruikt):
    #: alleen de dialect-variant heeft ``.overlap()`` in zijn comparator, en daarop draait
    #: het overlappredicaat waarmee ``claim_next_task`` bepaalt of twee taken elkaar in de
    #: weg kunnen zitten.
    affects_deployments: Mapped[list[str] | None] = mapped_column(PG_ARRAY(String(63)))

    __table_args__ = (
        Index("idx_async_tasks_pending", "status", "created_at", postgresql_where=text("status = 'pending'")),
        Index(
            "idx_async_tasks_heartbeat",
            "status",
            "heartbeat_at",
            postgresql_where=text("status IN ('claimed', 'running')"),
        ),
        Index("idx_async_tasks_project", text("project_name"), text("created_at DESC")),
        Index("idx_async_tasks_deployment", text("project_name"), text("deployment_name"), text("created_at DESC")),
        Index("idx_async_tasks_affects", "affects_deployments", postgresql_using="gin"),
        Index(
            "idx_async_tasks_completed",
            "status",
            "completed_at",
            postgresql_where=text("status IN ('completed', 'failed', 'cancelled')"),
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict; JSONB/array columns are already native, uuid/datetime are
        stringified, and ``id`` is aliased to ``task_id`` for API consistency."""
        d: dict[str, Any] = {}
        for col in self.__table__.columns:
            value = getattr(self, col.name)
            if isinstance(value, uuid.UUID):
                value = str(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            d[col.name] = value
        d["task_id"] = d["id"]
        return d
