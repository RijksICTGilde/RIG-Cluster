"""Config model for the ``metrics-scraper`` service (RC-5 Phase 2).

Component-level config. Note the on-disk shape is unusual: ``port``/``path`` sit
inline on the service value (``- metrics-scraper: {port: 8000, path: /metrics}``),
not under a ``config:`` wrapper (project_manager.py metrics handling). This model
describes those two fields; both are optional (the manager defaults them to None
and falls back to the application port / ``/metrics`` at render time).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opi.utils.url_paths import sanitize_path_value


class MetricsScraperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int | None = Field(
        default=None,
        ge=1024,
        le=65535,
        description=(
            "Port metrics are scraped from; the application port when left out. Must be 1024 or higher: "
            "images run non-root and cannot bind below that."
        ),
    )
    path: str | None = Field(
        default=None,
        description=(
            "Path metrics are scraped from; /metrics when left out. Only '/', letters, digits and '-._~'; "
            "the path ends up in the scrape config of the ServiceMonitor."
        ),
    )

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str | None) -> str | None:
        # Dezelfde tekenset als een component-path (opi/utils/url_paths.py), want het is
        # hetzelfde soort waarde: een pad van een tenant dat in een manifest terechtkomt.
        return None if value is None else sanitize_path_value(value, "path")
