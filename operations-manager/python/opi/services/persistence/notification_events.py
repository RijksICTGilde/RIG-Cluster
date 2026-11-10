"""ORM model for ``notification_events`` (migration 013): the platform's events.

Immutable: insert only. The one change allowed after writing is the pseudonymisation of
``actor`` and ``origin`` by the retention loop in ``opi/services/audit.py``, today the only
writer of this table.
"""

from __future__ import annotations

import uuid  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from typing import Any

from sqlalchemy import DateTime, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class NotificationEvent(Base):
    """One platform event. Immutable: insert only."""

    __tablename__ = "notification_events"

    # CloudEvents-shaped: id, source, specversion, type, subject, time. While ``source``
    # carries a placeholder this is NOT a valid CloudEvent and must not be called one.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    specversion: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'1.0'"))
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    #: ``<project>`` or ``<project>/<deployment>``, never an email address.
    subject: Mapped[str | None] = mapped_column(String(255))
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    # Ours, because the profile says nothing about these.
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    cluster: Mapped[str] = mapped_column(String(63), nullable=False)
    project: Mapped[str | None] = mapped_column(String(63))
    deployment: Mapped[str | None] = mapped_column(String(63))
    component: Mapped[str | None] = mapped_column(String(63))
    #: Personal data; after 90 days the retention loop sets this to NULL, never to a string.
    actor: Mapped[str | None] = mapped_column(String(255))
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: BIO2 8.15.01 "Oorsprong": client IP. Personal data, like ``actor``.
    origin: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    flow_id: Mapped[str | None] = mapped_column(String(64))
    #: ``async_tasks.id``, deliberately NOT a foreign key: that row is gone after an hour.
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    thread_key: Mapped[str | None] = mapped_column(String(255))
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))

    __table_args__ = (
        Index("idx_notification_events_dedup", "dedup_key", "time"),
        Index("idx_notification_events_project", text("project"), text("time DESC")),
        Index("idx_notification_events_thread", text("thread_key"), text("time DESC")),
        # The pseudonymisation pass's index: a processed row drops out of it by itself,
        # because the loop sets both columns to NULL.
        Index(
            "idx_notification_events_actor_sweep",
            "time",
            postgresql_where=text("actor IS NOT NULL OR origin IS NOT NULL"),
        ),
        # The retention pass's index; it cannot use the sweep index, because by then its
        # rows carry neither actor nor origin.
        Index("idx_notification_events_retention", "time"),
    )
