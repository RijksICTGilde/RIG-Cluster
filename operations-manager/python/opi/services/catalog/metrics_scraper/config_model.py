"""Config model for the ``metrics-scraper`` service (RC-5 Phase 2).

Component-level config. Note the on-disk shape is unusual: ``port``/``path`` sit
inline on the service value (``- metrics-scraper: {port: 8000, path: /metrics}``),
not under a ``config:`` wrapper (project_manager.py metrics handling). This model
describes those two fields; both are optional (the manager defaults them to None
and falls back to the application port / ``/metrics`` at render time).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MetricsScraperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Scrape target; when None the deployment falls back to the application port.
    # Non-privileged only (>=1024): images run non-root, so a scrape port below
    # 1024 can never be bound or reached.
    port: int | None = Field(default=None, ge=1024, le=65535)
    # Metrics path; when None the template falls back to "/metrics".
    path: str | None = None
