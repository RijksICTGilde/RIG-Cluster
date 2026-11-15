"""The contract the two metrics backends share.

``get_metrics_connector()`` returns ``PrometheusConnector`` or ``GrafanaPrometheusConnector``
depending on ``METRICS_BACKEND``. Type a connector as ``MetricsBackend`` and catch the
exceptions from here, not the ones of either backend.
"""

from datetime import UTC, datetime
from typing import Any, Protocol


class MetricsUnavailableError(Exception):
    """The backend is unreachable or refuses the credentials.

    Only the Grafana backend raises this: the direct Prometheus connector has no
    availability notion and reports every failure as ``MetricsQueryError``.
    """


class MetricsQueryError(Exception):
    """The query failed while the backend itself answered."""


def parse_query_time(value: str | int | float) -> datetime:
    """Parse a range-query time bound as Unix-epoch seconds or an ISO-8601 string."""
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    text = str(value).strip()
    try:
        return datetime.fromtimestamp(float(text), tz=UTC)
    except ValueError:
        return datetime.fromisoformat(text)


class MetricsBackend(Protocol):
    """Transport for PromQL. The queries themselves live in ``services/metrics_queries``."""

    is_connected: bool

    async def query(self, query: str) -> list[dict[str, Any]]: ...

    async def query_batch(self, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]: ...

    async def query_range_batch(
        self, queries: dict[str, str], start_time: datetime, end_time: datetime, step: str
    ) -> dict[str, list[dict[str, Any]]]: ...
