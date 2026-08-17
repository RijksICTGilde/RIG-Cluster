"""Tests voor de nachtelijke reconciliation-scheduler."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from opi.core.reconciliation_scheduler import ReconciliationScheduler, _seconds_until_next_run

TZ = ZoneInfo("Europe/Amsterdam")


def test_wacht_tot_vanavond_als_het_uur_nog_moet_komen() -> None:
    now = datetime(2026, 8, 17, 1, 0, tzinfo=TZ)
    assert _seconds_until_next_run(3, now) == 2 * 3600


def test_wacht_tot_morgen_als_het_uur_geweest_is() -> None:
    now = datetime(2026, 8, 17, 5, 30, tzinfo=TZ)
    assert _seconds_until_next_run(3, now) == 21.5 * 3600


@pytest.mark.asyncio
async def test_lege_store_slaat_de_run_over() -> None:
    """Een lege store betekent 'projecten nog niet geladen', niet 'alles is een wees'."""
    scheduler = ReconciliationScheduler(cluster="odcn-production")
    store = MagicMock()
    store.get_all = MagicMock(return_value=[])

    with (
        patch("opi.core.reconciliation_scheduler.get_project_store", return_value=store),
        patch("opi.jobs.reconciliation.reconcile", new=AsyncMock()) as mock_reconcile,
    ):
        result = await scheduler.run_once()

    mock_reconcile.assert_not_called()
    assert result == {"skipped": "empty project store"}


@pytest.mark.asyncio
async def test_draait_standaard_in_dry_run() -> None:
    """De scheduler mag pas echt verwijderen als iemand dat expliciet aanzet."""
    scheduler = ReconciliationScheduler(cluster="odcn-production")
    project = MagicMock()
    project.data = {"name": "wies", "deployments": []}
    store = MagicMock()
    store.get_all = MagicMock(return_value=[project])

    reconcile_mock = AsyncMock(return_value={"purged": [], "unmarked": [], "errors": []})
    with (
        patch("opi.core.reconciliation_scheduler.get_project_store", return_value=store),
        patch("opi.jobs.reconciliation.reconcile", new=reconcile_mock),
    ):
        await scheduler.run_once()

    assert reconcile_mock.await_args.kwargs["dry_run"] is True
    assert reconcile_mock.await_args.kwargs["project_yamls"] == [project.data]


@pytest.mark.asyncio
async def test_stoppen_zonder_gestart_te_zijn_is_veilig() -> None:
    scheduler = ReconciliationScheduler(cluster="odcn-production")
    await scheduler.stop()
