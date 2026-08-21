"""De bewaker die `user-applications` blijft verversen tot onze commit vergeleken is.

Een refresh op de app-of-apps is een vlaggetje, geen opdracht in een wachtrij: een al
lopende reconcile wist het als hij klaar is, terwijl die zijn revisie ophaalde voordat wij
pushten. Dan wacht OPI 360 seconden op een applicatie die pas bij de volgende reconcile
(op productie een kwartier later) aangemaakt wordt. Het enige harde bewijs dat de umbrella
onze wijziging gezien heeft is de revisie; `reconciledAt` kan nieuw zijn terwijl de
vergeleken revisie oud is.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.argo_manager import ArgoManager
from opi.manager.project_manager import ProjectManager

RECONCILED_AT = "2026-08-19T12:00:00Z"


class FakeArgoConnector:
    """Telt refreshes en serveert een reeks revisies (de laatste blijft gelden)."""

    def __init__(self, revisions: list[str | None]) -> None:
        self._revisions = list(revisions)
        self.refreshes = 0
        self.status_calls = 0

    async def refresh_application(self, app_name: str, hard_refresh: bool = False) -> str | None:
        assert app_name == "user-applications"
        self.refreshes += 1
        return RECONCILED_AT

    async def get_application_status(self, app_name: str) -> dict:
        self.status_calls += 1
        revision = self._revisions.pop(0) if len(self._revisions) > 1 else self._revisions[0]
        return {"status": {"sync": {"revision": revision}, "reconciledAt": RECONCILED_AT}}


def _watcher_self() -> SimpleNamespace:
    """Het minimum dat de bewaker van zijn ProjectManager gebruikt."""
    return SimpleNamespace(_argo_manager=SimpleNamespace(last_umbrella_revision=None, last_umbrella_reconciled_at=None))


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Vangt de wachttijden op zonder ze echt te wachten."""
    opgevangen: list[float] = []
    echte_sleep = asyncio.sleep

    async def _fake_sleep(delay: float, *args, **kwargs):
        opgevangen.append(delay)
        return await echte_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return opgevangen


async def test_ververst_opnieuw_tot_de_revisie_de_onze_is(sleeps: list[float]) -> None:
    connector = FakeArgoConnector(["oude-revisie", "oude-revisie", "onze-commit"])
    zelf = _watcher_self()

    await ProjectManager._keep_umbrella_refreshed(zelf, connector, "onze-commit")  # type: ignore[arg-type]

    # Eerste ronde plus twee herhalingen; daarna stopt hij, want de umbrella heeft onze
    # commit vergeleken en nog een refresh zou circa 90 child-apps hertekenen.
    assert connector.refreshes == 3
    assert zelf._argo_manager.last_umbrella_revision == "onze-commit"
    assert zelf._argo_manager.last_umbrella_reconciled_at == RECONCILED_AT


async def test_ververst_niet_opnieuw_als_de_umbrella_al_bij_is(sleeps: list[float]) -> None:
    connector = FakeArgoConnector(["onze-commit"])

    await ProjectManager._keep_umbrella_refreshed(_watcher_self(), connector, "onze-commit")  # type: ignore[arg-type]

    assert connector.refreshes == 1, "alleen de eerste ronde"


async def test_wacht_hoogstens_de_ondergrens_tussen_twee_refreshes(sleeps: list[float]) -> None:
    """De lus loopt op het antwoord van de refresh; de ondergrens is alleen een rem."""
    from opi.manager.project_manager import UMBRELLA_REFRESH_MIN_INTERVAL_SECONDEN

    connector = FakeArgoConnector(["oude-revisie", "oude-revisie", "onze-commit"])

    await ProjectManager._keep_umbrella_refreshed(_watcher_self(), connector, "onze-commit")  # type: ignore[arg-type]

    assert len(sleeps) == connector.refreshes - 1, "alleen tússen twee refreshes wordt gewacht"
    assert all(0 < wachttijd <= UMBRELLA_REFRESH_MIN_INTERVAL_SECONDEN for wachttijd in sleeps), (
        "nooit langer dan de ondergrens, want de refresh wachtte zelf al op een reconcile"
    )


async def test_stopt_na_het_maximum_aantal_pogingen(sleeps: list[float]) -> None:
    """Blijft de revisie verkeerd, dan is er iets anders aan de hand dan een verloren wekker."""
    from opi.manager.project_manager import UMBRELLA_REFRESH_MAX_POGINGEN

    connector = FakeArgoConnector(["oude-revisie"])

    await ProjectManager._keep_umbrella_refreshed(_watcher_self(), connector, "onze-commit")  # type: ignore[arg-type]

    assert connector.refreshes == UMBRELLA_REFRESH_MAX_POGINGEN, "niet eindeloos doorprikken"


async def test_zonder_bekende_commit_blijft_het_bij_een_refresh(sleeps: list[float]) -> None:
    connector = FakeArgoConnector(["oude-revisie"])

    await ProjectManager._keep_umbrella_refreshed(_watcher_self(), connector, None)  # type: ignore[arg-type]

    assert connector.refreshes == 1
    assert connector.status_calls == 0, "zonder bewijs niet gokken"


async def test_drie_wachtende_applicaties_delen_een_bewaker(sleeps: list[float]) -> None:
    """Eén bewaker naast de wachters, niet één per applicatie."""
    connector = FakeArgoConnector(["oude-revisie", "oude-revisie", "onze-commit"])
    watcher = asyncio.create_task(
        ProjectManager._keep_umbrella_refreshed(_watcher_self(), connector, "onze-commit")  # type: ignore[arg-type]
    )

    async def _wacht_op_applicatie(_naam: str) -> None:
        # Drie wachters die tegelijk pollen; geen van hen prikt zelf de umbrella.
        while not watcher.done():
            await asyncio.sleep(1)

    await asyncio.gather(*(_wacht_op_applicatie(naam) for naam in ("app-1", "app-2", "app-3")))
    await watcher

    assert connector.refreshes == 3, "drie rondes, geen drie refreshes per ronde"


async def test_timeoutmelding_noemt_de_commit_en_de_gelezen_revisie() -> None:
    manager = ArgoManager(MagicMock())
    manager.last_pushed_argo_commit = "abc123def456"
    manager.last_umbrella_revision = "oude-revisie"
    manager.last_umbrella_reconciled_at = RECONCILED_AT

    connector = AsyncMock()
    connector.login = AsyncMock(return_value=True)
    connector.application_exists = AsyncMock(return_value=False)

    with patch("opi.connectors.argo.ArgoConnector", return_value=connector), pytest.raises(TimeoutError) as excinfo:
        await manager.wait_for_application_created("rig-x-main", timeout=0, poll_interval=1)

    melding = str(excinfo.value)
    assert "rig-x-main" in melding
    assert "abc123def456" in melding
    assert "oude-revisie" in melding
    assert RECONCILED_AT in melding
