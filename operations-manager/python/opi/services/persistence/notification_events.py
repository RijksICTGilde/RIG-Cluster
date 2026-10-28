"""ORM-model voor ``notification_events`` (migratie 013): de gebeurtenissen van het platform.

Onveranderlijk: alleen invoegen. De enige toegestane wijziging na het schrijven is de
pseudonimisering van ``actor`` en ``origin`` door de bewaarlus in
``opi/services/audit.py``. De audit is vandaag de enige schrijver; het datamodel komt uit
``features/futures/gebeurtenissen-vastleggen-en-melden.md`` punt 7.
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
    """Een gebeurtenis op het platform. Onveranderlijk: alleen invoegen."""

    __tablename__ = "notification_events"

    # CloudEvents-vormig: id, source, specversion, type, subject, time. Zolang ``source``
    # een placeholder draagt is dit GEEN geldig CloudEvent en mag het ook niet zo heten.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    specversion: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'1.0'"))
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    #: ``<project>`` of ``<project>/<deployment>``, nooit een e-mailadres.
    subject: Mapped[str | None] = mapped_column(String(255))
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    # Van ons, want het profiel zegt er niets over.
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    cluster: Mapped[str] = mapped_column(String(63), nullable=False)
    project: Mapped[str | None] = mapped_column(String(63))
    deployment: Mapped[str | None] = mapped_column(String(63))
    component: Mapped[str | None] = mapped_column(String(63))
    #: Persoonsgegeven; de bewaarlus zet hem na 90 dagen op NULL, nooit op een tekst.
    actor: Mapped[str | None] = mapped_column(String(255))
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: BIO2 8.15.01 "Oorsprong": client-IP. Persoonsgegeven, net als ``actor``.
    origin: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    flow_id: Mapped[str | None] = mapped_column(String(64))
    #: ``async_tasks.id``, met opzet GEEN vreemde sleutel: die rij verdwijnt na een uur.
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    thread_key: Mapped[str | None] = mapped_column(String(255))
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))

    __table_args__ = (
        Index("idx_notification_events_dedup", "dedup_key", "time"),
        Index("idx_notification_events_project", text("project"), text("time DESC")),
        Index("idx_notification_events_thread", text("thread_key"), text("time DESC")),
        # De index van de pseudonimiseringspas: een verwerkte rij valt er vanzelf uit,
        # want de lus zet beide kolommen op NULL.
        Index(
            "idx_notification_events_actor_sweep",
            "time",
            postgresql_where=text("actor IS NOT NULL OR origin IS NOT NULL"),
        ),
        # De index van de opruimpas, die de sweep-index niet kan gebruiken: zijn rijen
        # dragen dan geen actor en geen origin meer.
        Index("idx_notification_events_retention", "time"),
    )
