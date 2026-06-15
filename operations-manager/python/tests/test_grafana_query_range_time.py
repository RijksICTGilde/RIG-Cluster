"""Regression test for the GrafanaPrometheusConnector range-query time contract.

When prod switched METRICS_BACKEND=grafana, range queries started routing through
GrafanaPrometheusConnector.query_range, whose time parsing assumed ISO-8601 only
(`datetime.fromisoformat`). The metrics-UI callers pass Unix-epoch seconds (e.g.
"1781530694"), so every range query raised "Invalid isoformat string" - which also
starved auto-tune of metrics. The direct PrometheusConnector documents accepting
"RFC3339 or Unix timestamp", so the two interchangeable connectors must agree.
"""

from datetime import UTC, datetime

import pytest
from opi.connectors.grafana_prometheus import _parse_query_time


def test_parses_epoch_seconds_string() -> None:
    # The exact value that raised in production.
    assert _parse_query_time("1781530694") == datetime.fromtimestamp(1781530694, tz=UTC)


def test_parses_epoch_int_and_float() -> None:
    assert _parse_query_time(1781530694) == datetime.fromtimestamp(1781530694, tz=UTC)
    assert _parse_query_time(1781530694.0) == datetime.fromtimestamp(1781530694.0, tz=UTC)


def test_parses_iso8601_naive_and_aware() -> None:
    assert _parse_query_time("2026-06-15T16:08:00") == datetime.fromisoformat("2026-06-15T16:08:00")
    assert _parse_query_time("2026-06-15T16:08:00+00:00") == datetime.fromisoformat("2026-06-15T16:08:00+00:00")


def test_rejects_unparseable() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        _parse_query_time("not-a-timestamp")
