"""Capacity watcher: thresholds, growth, and one notice per quota per level."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.connectors.metrics_backend import MetricsQueryError, MetricsUnavailableError
from opi.runtime.periodic import capacity_watcher
from opi.runtime.periodic.capacity_watcher import HIGH, OK, WARNING, CapacityWatcher, Quota, evaluate

GIB = 1024**3


def _q(resource: str, used: float, limit: float, ago: float | None = None) -> Quota:
    return Quota(resource=resource, used=used, limit=limit, used_24h_ago=used if ago is None else ago)


@pytest.mark.parametrize(
    ("quota", "level"),
    [
        (_q("cpu-limits", 200, 320), OK),
        (_q("cpu-limits", 290, 320), WARNING),
        (_q("cpu-limits", 310, 320), HIGH),
        (_q("pod-count-limit", 500, 600), OK),
    ],
)
def test_percentage_thresholds(quota: Quota, level: int) -> None:
    assert evaluate(quota)[0] == level


def test_little_free_cpu_or_memory_warns_below_the_percentage() -> None:
    # 87.5% is below the 90% threshold, but only 4 cores are left.
    assert evaluate(_q("cpu-requests", 28, 32))[0] == WARNING
    assert evaluate(_q("memory-limits", 44 * GIB, 48 * GIB))[0] == WARNING
    assert evaluate(_q("memory-limits", 100 * GIB, 240 * GIB))[0] == OK


def test_absolute_free_rule_skips_other_resources() -> None:
    assert evaluate(_q("initc-cpu-limits", 28, 32))[0] == OK
    assert evaluate(_q("storage", 396 * GIB, 400 * GIB))[0] == HIGH  # via the percentage only


def test_growth_in_points_or_relative() -> None:
    assert evaluate(_q("cpu-limits", 160, 320, ago=120))[0] == WARNING  # +12.5 points
    assert evaluate(_q("memory-requests", 30 * GIB, 120 * GIB, ago=22 * GIB))[0] == WARNING  # +36%, at 25%
    assert evaluate(_q("cpu-requests", 6, 48, ago=4))[0] == OK  # +50%, but only at 12.5%
    assert evaluate(Quota("cpu-limits", 160, 320, None))[0] == OK  # no history yet


async def test_notifies_once_per_level_and_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    watcher = CapacityWatcher()
    send = AsyncMock()
    monkeypatch.setattr(watcher, "_send", send)
    readings = iter(
        [
            [_q("cpu-limits", 290, 320)],  # warning: notify
            [_q("cpu-limits", 291, 320)],  # still warning: quiet
            [_q("cpu-limits", 310, 320)],  # high: notify
            [_q("cpu-limits", 200, 320)],  # ok: re-arm
            [_q("cpu-limits", 290, 320)],  # warning again: notify
        ]
    )
    monkeypatch.setattr(watcher, "_fetch", AsyncMock(side_effect=lambda: next(readings)))

    for _ in range(5):
        await watcher.tick()

    assert [c.args[1] for c in send.await_args_list] == ["default", "high", "default"]
    assert "cpu-limits: van 290.0 naar 290.0, maximaal 320, 91% in gebruik" in send.await_args_list[0].args[0]


def test_format_shows_yesterday_today_and_the_limit() -> None:
    assert capacity_watcher._format(_q("cpu-limits", 180, 500, ago=170)) == "van 170.0 naar 180.0, maximaal 500"
    assert (
        capacity_watcher._format(_q("memory-limits", 200 * GIB, 240 * GIB, ago=150 * GIB))
        == "van 150 naar 200 GiB, maximaal 240 GiB"
    )
    assert capacity_watcher._format(Quota("memory-limits", 200 * GIB, 240 * GIB, None)) == "200 van 240 GiB"


def test_growth_reason_is_relative_to_yesterday() -> None:
    assert evaluate(_q("cpu-limits", 160, 320, ago=120))[1] == ["stijging 33% in 24 uur"]
    assert evaluate(_q("services-lb", 1, 3, ago=0))[1] == ["stijging in 24 uur"]


@pytest.mark.parametrize("fout", [MetricsUnavailableError("niet beschikbaar"), MetricsQueryError("status 500")])
async def test_tick_slaat_de_ronde_over_zonder_metrics(fout: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    """Een storing in de metingsbron is een overgeslagen ronde, geen crash van de watcher."""
    watcher = CapacityWatcher()
    send = AsyncMock()
    monkeypatch.setattr(watcher, "_send", send)
    monkeypatch.setattr(watcher, "_fetch", AsyncMock(side_effect=fout))

    await watcher.tick()

    send.assert_not_awaited()


async def test_tick_laat_andere_fouten_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alleen de twee metingsfouten worden afgevangen; een andere fout moet zichtbaar blijven."""
    watcher = CapacityWatcher()
    monkeypatch.setattr(watcher, "_fetch", AsyncMock(side_effect=RuntimeError("iets anders")))

    with pytest.raises(RuntimeError, match="iets anders"):
        await watcher.tick()


async def test_fetch_queries_the_billing_datasource_and_builds_quotas(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_fetch`` must call the renamed ``query``, with the billing datasource.

    A resource present only in the limit query and not in the usage query is dropped:
    with no usage there is nothing to report.
    """
    monkeypatch.setattr(capacity_watcher.settings, "CAPACITY_WATCH_TENANT", "rig-demo")
    monkeypatch.setattr(capacity_watcher.settings, "GRAFANA_BILLING_DATASOURCE_UID", "billing-uid")

    def answer(query: str, *, datasource_uid: str | None = None) -> list[dict[str, Any]]:
        assert datasource_uid == "billing-uid"
        if "offset 24h" in query:
            return [{"metric": {"resource": "cpu-limits"}, "value": [0, "180"]}]
        if "_limit{" in query:
            return [
                {"metric": {"resource": "cpu-limits"}, "value": [0, "320"]},
                {"metric": {"resource": "orphan-limit-only"}, "value": [0, "10"]},
            ]
        return [{"metric": {"resource": "cpu-limits"}, "value": [0, "200"]}]

    connector = AsyncMock()
    connector.query.side_effect = answer
    monkeypatch.setattr(capacity_watcher, "create_grafana_prometheus_connector", AsyncMock(return_value=connector))

    quotas = await CapacityWatcher()._fetch()

    assert quotas == [Quota(resource="cpu-limits", used=200.0, limit=320.0, used_24h_ago=180.0)]
    queried = [call.args[0] for call in connector.query.call_args_list]
    assert any("_resource_usage" in q and "offset 24h" not in q for q in queried)
    assert any("_resource_limit" in q for q in queried)
    assert any("offset 24h" in q for q in queried)
