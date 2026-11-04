"""The Grafana connector's availability status, with a MockTransport instead of Grafana.

Measures what the 4 November 2026 outage needed: a boot that does not wait for Grafana,
queries that fail fast while it is down, a recovery task that keeps trying, and one HTTP
client through all of it.
"""

import asyncio

import httpx
import pytest
from opi.connectors.grafana_prometheus import (
    GrafanaConnectionError,
    GrafanaPrometheusConnector,
    GrafanaQueryError,
)


class Backend:
    """A Grafana that answers, refuses or errors, and counts the requests it saw."""

    def __init__(self, failures: int = 0, status: int = 200) -> None:
        self.failures = failures
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures > 0:
            self.failures -= 1
            raise httpx.ConnectError("connection refused", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        return httpx.Response(
            200,
            json={
                "results": {
                    "A": {
                        "frames": [
                            {
                                "schema": {"fields": [{}, {"labels": {}}]},
                                "data": {"values": [[1000], [1.0]]},
                            }
                        ]
                    }
                }
            },
        )


@pytest.fixture
async def connector(monkeypatch):
    """A fresh connector singleton; the backend is attached per test."""
    GrafanaPrometheusConnector._instance = None
    monkeypatch.setattr("opi.core.config.settings.GRAFANA_TOKEN", "test-token")
    monkeypatch.setattr("opi.core.config.settings.GRAFANA_DATASOURCE_UID", "test-uid")
    c = GrafanaPrometheusConnector()
    # Short waits: the real sequence is 5s to 60s, which no test can sit out.
    c._recovery_delays = (0.01,)
    yield c
    await c.aclose()
    GrafanaPrometheusConnector._instance = None
    GrafanaPrometheusConnector.is_connected = False


def attach(connector: GrafanaPrometheusConnector, backend: Backend) -> None:
    connector._client = httpx.AsyncClient(transport=backend.transport())


@pytest.mark.asyncio
async def test_startup_does_no_io(connector):
    """The startup call returns at once and makes no request of its own."""
    backend = Backend(failures=99)
    attach(connector, backend)

    connector.start_probe()

    assert connector.is_connected is False
    assert backend.requests == []


@pytest.mark.asyncio
async def test_transport_error_marks_unavailable_and_starts_one_task(connector):
    """A failed query flips the status and starts exactly one recovery task."""
    backend = Backend(failures=99)
    attach(connector, backend)
    connector._mark_available()

    with pytest.raises(GrafanaConnectionError):
        await connector.custom_query("up")

    assert connector.is_connected is False
    first_task = connector._recovery_task
    assert first_task is not None

    for _ in range(3):
        connector._mark_unavailable("again")
    assert connector._recovery_task is first_task


@pytest.mark.asyncio
async def test_queries_fail_fast_without_a_request(connector):
    """While unavailable, every query raises before touching the network."""
    backend = Backend()
    attach(connector, backend)
    # Long enough that the recovery task cannot sneak a request into this measurement.
    connector._recovery_delays = (3600.0,)
    connector._mark_unavailable("outage")
    seen = len(backend.requests)

    for _ in range(10):
        with pytest.raises(GrafanaConnectionError):
            await connector.custom_query("up")

    assert len(backend.requests) == seen


@pytest.mark.asyncio
async def test_bad_request_leaves_the_status_alone(connector):
    """An HTTP 400 is a broken query, not a broken backend."""
    backend = Backend(status=400)
    attach(connector, backend)
    connector._mark_available()

    with pytest.raises(GrafanaQueryError):
        await connector.custom_query("not promql")

    assert connector.is_connected is True
    assert connector._recovery_task is None


@pytest.mark.asyncio
async def test_recovery_comes_back_and_stops(connector):
    """Three refusals, then an answer: the task restores the status and ends."""
    backend = Backend(failures=3)
    attach(connector, backend)

    connector.start_probe()
    await asyncio.wait_for(connector._recovery_task, timeout=5)

    assert connector.is_connected is True
    assert connector._recovery_task.done()


@pytest.mark.asyncio
async def test_recovery_never_gives_up(connector):
    """Past ten failed probes the task is still running."""
    backend = Backend(failures=99)
    attach(connector, backend)

    connector.start_probe()
    while len(backend.requests) <= 10:
        await asyncio.sleep(0.01)

    assert connector._recovery_task.done() is False
    assert connector.is_connected is False


@pytest.mark.asyncio
async def test_one_client_through_outage_and_recovery(connector):
    """The HTTP client is never replaced, so an outage leaks no connection pools."""
    backend = Backend(failures=2)
    attach(connector, backend)
    client = connector._client

    connector.start_probe()
    await asyncio.wait_for(connector._recovery_task, timeout=5)
    await connector.custom_query("up")

    assert connector._client is client


@pytest.mark.asyncio
async def test_aclose_cancels_the_task_and_closes_the_client(connector):
    """Shutdown leaves no pending task and no open client."""
    backend = Backend(failures=99)
    attach(connector, backend)
    connector.start_probe()
    task = connector._recovery_task

    await connector.aclose()

    assert task.cancelled()
    assert connector._recovery_task is None
    assert connector._client.is_closed


@pytest.mark.asyncio
async def test_bad_request_does_not_mark_available(connector):
    """A 400 while unavailable keeps the status unavailable: only a 2xx restores it."""
    backend = Backend(status=400)
    attach(connector, backend)
    connector._recovery_delays = (3600.0,)
    connector._mark_unavailable("outage")

    resp = await connector._request("POST", "/api/ds/query", {})

    assert resp.status_code == 400
    assert connector.is_connected is False


@pytest.mark.asyncio
async def test_discovery_without_datasource_logs_no_false_recovery(connector, monkeypatch, caplog):
    """A datasource list without prometheus or mimir never reads as "available"."""
    monkeypatch.setattr(connector, "_datasource_uid", None)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"type": "loki", "uid": "logs", "name": "Loki"}])

    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    with caplog.at_level("INFO", logger="opi.connectors.grafana_prometheus"):
        connector.start_probe()
        while len(requests) < 5:
            await asyncio.sleep(0.01)

    assert connector.is_connected is False
    messages = [r.getMessage() for r in caplog.records if r.levelname in ("INFO", "WARNING")]
    assert not any("available again" in m or m == "Grafana metrics are available" for m in messages)
    assert sum("unavailable" in m for m in messages) == 1
