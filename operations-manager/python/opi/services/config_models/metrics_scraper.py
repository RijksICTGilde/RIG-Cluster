"""Config model for the ``metrics-scraper`` service (RC-5 Phase 2).

Component-level config. Note the on-disk shape is unusual: ``port``/``path`` sit
inline on the service value (``- metrics-scraper: {port: 8000, path: /metrics}``),
not under a ``config:`` wrapper (project_manager.py metrics handling). This model
describes those two fields; both are optional (the manager defaults them to None
and falls back to the application port / ``/metrics`` at render time).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class MetricsScraperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Scrape target; when None the deployment falls back to the application port.
    port: int | None = None
    # Metrics path; when None the template falls back to "/metrics".
    path: str | None = None
