"""Tests for the nightly resource tuning scheduler's run-time computation."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

from opi.core.actor import scheduler_actor
from opi.core.resource_tuning_scheduler import ResourceTuningScheduler, _seconds_until_next_run


class TestSecondsUntilNextRun:
    def test_later_today(self):
        # 00:00, target 02:00 -> 2 hours
        now = datetime(2026, 6, 23, 0, 0, 0, tzinfo=UTC)
        assert _seconds_until_next_run(2, now) == 2 * 3600

    def test_already_passed_rolls_to_tomorrow(self):
        # 03:00, target 02:00 already passed -> 23 hours until tomorrow 02:00
        now = datetime(2026, 6, 23, 3, 0, 0, tzinfo=UTC)
        assert _seconds_until_next_run(2, now) == 23 * 3600

    def test_exactly_on_the_hour_waits_full_day(self):
        # 02:00 exactly -> target is not in the future -> tomorrow
        now = datetime(2026, 6, 23, 2, 0, 0, tzinfo=UTC)
        assert _seconds_until_next_run(2, now) == 24 * 3600

    def test_partial_hour(self):
        # 01:30, target 02:00 -> 30 minutes
        now = datetime(2026, 6, 23, 1, 30, 0, tzinfo=UTC)
        assert _seconds_until_next_run(2, now) == 1800


async def test_run_zet_de_scheduler_actor_voor_de_lus() -> None:
    scheduler = ResourceTuningScheduler(cluster="odcn-production")

    with patch("opi.core.resource_tuning_scheduler.set_actor") as mock_set_actor:
        await scheduler.start()
        await asyncio.sleep(0)
        await scheduler.stop()

    mock_set_actor.assert_called_once_with(scheduler_actor("resource-tuning"))
