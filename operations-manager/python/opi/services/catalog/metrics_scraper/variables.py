"""Environment variables the metrics-scraper service provides -- single source of truth.

Lives in the service's own package (RC-36): what a service hands to a deployment
is part of that service, not of a shared module.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class MetricsScraperVariables(Enum):
    """Metrics scraper service variable definitions."""

    AUTH_TOKEN = VariableDefinition(
        name="METRICS_AUTH_TOKEN",
        description="Bearer token that Prometheus sends when scraping /metrics. Validate this to restrict access.",
        source="secret",
        secret_key="token",
        aliases=["PROMETHEUS_METRICS_AUTH_TOKEN"],
    )
