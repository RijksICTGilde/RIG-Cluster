"""ORM model for ``api_step_ups``: the step-up of a bearer token.

The key is the credential and not the user: a step-up in the CLI must not open another
session or token of the same person.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class ApiStepUp(Base):
    """The last valid code given with this credential."""

    __tablename__ = "api_step_ups"

    credential: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    stepped_up_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
