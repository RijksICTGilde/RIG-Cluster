"""Capacity watcher: thresholds, growth, and one notice per quota per level."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from opi.core import capacity_watcher
from opi.core.capacity_watcher import HIGH, OK, WARNING, CapacityWatcher, Quota, evaluate

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
    assert "cpu-limits: 290.0 van 320" in send.await_args_list[0].args[0]


def test_memory_is_shown_in_gib() -> None:
    assert capacity_watcher._format(_q("memory-limits", 200 * GIB, 240 * GIB)) == "200 van 240 GiB"
