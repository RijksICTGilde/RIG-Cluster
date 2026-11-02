"""Regression test for PrometheusConnector.query_range, the sandbox metrics path.

prometheus_api_client calls ``.timestamp()`` on the bounds, and the dashboard passes
epoch strings: every range query failed and the network chart stayed empty. The method
was also sync while the dashboard awaits it.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from opi.connectors.prometheus import PrometheusConnector


async def test_query_range_passes_datetimes_and_is_awaitable() -> None:
    connector = PrometheusConnector()
    connector.prom = MagicMock()
    connector.prom.custom_query_range.return_value = [{"values": []}]

    result = await connector.query_range("up", start_time="1781530694", end_time="1781532494", step="300")

    assert result == [{"values": []}]
    kwargs = connector.prom.custom_query_range.call_args.kwargs
    assert kwargs["start_time"] == datetime.fromtimestamp(1781530694, tz=UTC)
    assert kwargs["end_time"] == datetime.fromtimestamp(1781532494, tz=UTC)
