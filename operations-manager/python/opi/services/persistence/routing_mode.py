"""ORM model for ``routing_mode`` (migration 016).

Written and read by ``RoutingModeWatcher``: the stand it last adopted and reprocessed every
project for. A restart must not reprocess again for a stand that is already in this table,
and must still reprocess for one that is not.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class RoutingMode(Base):
    """The routing stand this cluster has been brought to."""

    __tablename__ = "routing_mode"

    cluster: Mapped[str] = mapped_column(Text, primary_key=True)
    routing: Mapped[str] = mapped_column(Text, nullable=False)
    adopted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
