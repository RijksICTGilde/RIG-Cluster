"""Een wekactie rondt zichzelf af, in plaats van een half uur op de sweeper te wachten.

Gemeten op productie op 12-10-2026 (``wies/pr-689``): de app-pods stonden binnen een
minuut op 1/1, maar de wektaak liep 300 seconden door tot ``Timed out waiting for sync``
en de UI toonde nog een half uur "wordt gewekt". In ``waking`` kan de ArgoCD-applicatie
namelijk nooit Healthy worden: voor de app terug is, is de app niet ready, en daarna de
wekker niet (zijn readiness is met opzet omgekeerd).

Deze tests houden de nieuwe afronding vast: de eerste reprocess wacht niet op gezondheid,
``finish_wake`` wacht op de app zelf, zet ``awake`` en laat de tweede reprocess wel op
gezondheid wachten, en de sweeper blijft het vangnet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.catalog.sleep_mode import flow
from opi.services.catalog.sleep_mode import state as sleep_state

PROJECT = "wies"
DEPLOYMENT = "pr-689"
CLUSTER = "local"


def _progress() -> Any:
    """Een echte voortgangsmanager zonder zijn periodieke flush-lus."""
    with patch(
        "opi.core.persistent_task_progress.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        from opi.core.persistent_task_progress import PersistentTaskProgressManager

        return PersistentTaskProgressManager(
            task_id="task-under-test",
            project_name=PROJECT,
            task_service=MagicMock(),
        )


def _names(progress: Any) -> list[str]:
    return [info["name"] for info in progress._subtasks.values()]


def _project_data(sleep: dict | None) -> dict:
    """Een projectbestand met sleep-mode aan en een wekbare frontend."""
    deployment: dict[str, Any] = {
        "name": DEPLOYMENT,
        "cluster": CLUSTER,
        "namespace": "wies",
        "components": [{"reference": "frontend"}, {"reference": "worker"}],
    }
    if sleep is not None:
        deployment["sleep"] = sleep
    return {
        "name": PROJECT,
        "services": [{"name": "sleep-mode", "config": {"enabled": True, "match": ["pr-*"]}}],
        "components": [
            {"name": "frontend", "services": ["publish-on-web"]},
            {"name": "worker", "services": []},
        ],
        "deployments": [deployment],
    }


class _Omgeving:
    """Wat de wekpaden aanraken: de store, de ProjectManager, kubectl, de reprocess."""

    def __init__(self) -> None:
        self.save = AsyncMock(return_value=None)
        self.reprocess = AsyncMock(return_value=True)
        self.kubectl_calls: list[tuple[str, str]] = []

    @property
    def commits(self) -> list[str]:
        return [call.args[1] for call in self.save.await_args_list]

    @property
    def reprocess_calls(self) -> list[dict]:
        return [call.kwargs for call in self.reprocess.await_args_list]


def _omgeving(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cached: dict,
    fresh: dict | None = None,
    ready: list[bool],
) -> _Omgeving:
    """Zet de wekpaden op: ``cached`` is de storekopie, ``fresh`` wat git teruggeeft.

    ``ready`` is de reeks antwoorden van de app-Deployment op kubectl, zodat een app die
    eerst niet en daarna wel terug is gewoon een lijst is.
    """
    omgeving = _Omgeving()

    project = MagicMock()
    project.name = PROJECT
    project.filename = f"{PROJECT}.yaml"
    project.data = cached
    store = MagicMock()
    store.get.return_value = project
    monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: store)

    manager = AsyncMock()
    manager.get_contents = AsyncMock(return_value=fresh if fresh is not None else cached)
    manager.save_and_commit_project = omgeving.save
    manager.close = AsyncMock(return_value=None)
    monkeypatch.setattr("opi.manager.project_manager.ProjectManager", lambda **kwargs: manager)

    monkeypatch.setattr("opi.services.resource_tuning_service.trigger_reprocessing", omgeving.reprocess)

    antwoorden = list(ready)

    async def _status(namespace: str, name: str) -> list[dict]:
        omgeving.kubectl_calls.append((namespace, name))
        gereed = antwoorden.pop(0) if len(antwoorden) > 1 else antwoorden[0]
        return [{"ready": "1/1" if gereed else "0/1"}]

    connector = MagicMock()
    connector.get_deployment_status = AsyncMock(side_effect=_status)
    monkeypatch.setattr("opi.connectors.kubectl.KubectlConnector", lambda *a, **k: connector)

    # Geen echte pauze tussen twee gereedheidsvragen.
    monkeypatch.setattr(flow, "WAKE_POLL_SECONDS", 0)
    return omgeving


async def _wektaak(progress: Any) -> dict:
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    return await handle_sleep_transition(
        {"project_name": PROJECT, "deployment_name": DEPLOYMENT, "direction": "wake"}, progress
    )


# --- 1. Het scenario uit productie ----------------------------------------------------


@pytest.mark.asyncio
async def test_de_wektaak_eindigt_awake_zonder_op_een_gezondheid_te_wachten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De app is ready, de wekker (met opzet) niet: de taak slaagt en eindigt ``awake``.

    Dit is het gemeten geval. Een gedeelde dict staat voor het ene projectbestand: de
    wek schrijft er ``waking`` in, de afronding leest hem terug en schrijft ``awake``.
    """
    data = _project_data({"state": "sleeping", "wake-token": "x"})
    omgeving = _omgeving(monkeypatch, cached=data, ready=[True])
    progress = _progress()

    result = await _wektaak(progress)

    assert result["changed"] is True
    assert result["state"] == "awake", result
    assert sleep_state.read(data, DEPLOYMENT).state == "awake"

    assert omgeving.commits == [
        f"sleep-mode: wake {DEPLOYMENT} in {PROJECT}",
        f"sleep-mode: finish wake {DEPLOYMENT} in {PROJECT}",
    ]

    eerste, tweede = omgeving.reprocess_calls
    assert eerste["wait_for_healthy"] is False, "in waking wordt de app nooit Healthy"
    assert tweede.get("wait_for_healthy", True) is True, "de wekker is weg, dus dit wachten kan juist wel"

    # De stappen die de gebruiker ziet, inclusief de tweede helft.
    assert _names(progress) == [
        "Deployment wekken",
        "Projectgegevens ophalen",
        "Wektoestand vastleggen in git",
        "Wachten tot de applicatie opgestart is",
        "Wektoestand afronden in git",
    ]


# --- 2. De app komt eerst niet en daarna wel ------------------------------------------


@pytest.mark.asyncio
async def test_finish_wake_wacht_tot_de_app_er_is(monkeypatch: pytest.MonkeyPatch) -> None:
    waking = {"state": "waking", "expires-at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()}
    data = _project_data(waking)
    omgeving = _omgeving(monkeypatch, cached=data, ready=[False, False, True])

    result = await flow.finish_wake(PROJECT, DEPLOYMENT)

    assert result.changed is True
    assert result.state == "awake"
    assert len(omgeving.kubectl_calls) == 3, "er is doorgevraagd tot de app er was"
    # De gereedheid wordt op de app-Deployment van de wekbare component gelezen.
    assert omgeving.kubectl_calls[0] == ("rig-wies", "pr-689-frontend")
    assert len(omgeving.commits) == 1


# --- 3. De app komt niet terug voor de deadline ---------------------------------------


@pytest.mark.asyncio
async def test_een_app_die_niet_terugkomt_laat_de_taak_falen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Niets wijzigen: de toestand blijft ``waking`` en de sweeper doet daarna REVERT."""
    from opi.core.config import settings

    monkeypatch.setattr(settings, "SLEEP_MODE_WAKING_TIMEOUT_MINUTES", 0)
    data = _project_data({"state": "sleeping", "wake-token": "x"})
    omgeving = _omgeving(monkeypatch, cached=data, ready=[False])
    progress = _progress()

    with pytest.raises(flow.WakeTimeout) as exc:
        await _wektaak(progress)

    assert "de slaapstand-sweeper zet de deployment terug op wakker" in str(exc.value)
    assert sleep_state.read(data, DEPLOYMENT).state == "waking", "niets teruggedraaid, dat doet de sweeper"
    # Alleen de wek-commit; de afronding heeft niets geschreven.
    assert omgeving.commits == [f"sleep-mode: wake {DEPLOYMENT} in {PROJECT}"]
    assert len(omgeving.reprocess_calls) == 1
    assert "Wektoestand afronden in git" not in _names(progress)


@pytest.mark.asyncio
async def test_een_onleesbare_deadline_houdt_de_wacht_toch_begrensd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``begin_wake`` stempelt die deadline altijd; dit is de grens als hij er niet staat."""
    from opi.core.config import settings

    monkeypatch.setattr(settings, "SLEEP_MODE_WAKING_TIMEOUT_MINUTES", 0)
    data = _project_data({"state": "waking", "expires-at": "ooit"})
    omgeving = _omgeving(monkeypatch, cached=data, ready=[False])

    with pytest.raises(flow.WakeTimeout):
        await flow.finish_wake(PROJECT, DEPLOYMENT)

    assert omgeving.commits == []


# --- 4. De sweeper was eerder ---------------------------------------------------------


@pytest.mark.asyncio
async def test_als_de_sweeper_eerder_was_doet_finish_wake_niets(monkeypatch: pytest.MonkeyPatch) -> None:
    cached = _project_data({"state": "waking", "expires-at": (datetime.now(UTC) + timedelta(minutes=9)).isoformat()})
    fresh = _project_data(None)
    omgeving = _omgeving(monkeypatch, cached=cached, fresh=fresh, ready=[True])

    result = await flow.finish_wake(PROJECT, DEPLOYMENT)

    assert result.changed is False
    assert result.state == "awake"
    assert omgeving.commits == []
    assert omgeving.reprocess_calls == []


@pytest.mark.asyncio
async def test_een_deployment_die_intussen_weer_slaapt_wordt_niet_gewekt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``to_awake`` wekt ook uit ``sleeping``, dus de toestand uit git is de grendel.

    Zonder die her-lezing zou een deployment die tijdens het wachten met de hand in
    slaapstand is gezet hier alsnog op ``awake`` worden gezet.
    """
    cached = _project_data({"state": "waking", "expires-at": (datetime.now(UTC) + timedelta(minutes=9)).isoformat()})
    fresh = _project_data({"state": "sleeping", "wake-token": "x"})
    omgeving = _omgeving(monkeypatch, cached=cached, fresh=fresh, ready=[True])

    result = await flow.finish_wake(PROJECT, DEPLOYMENT)

    assert (result.changed, result.state) == (False, "sleeping")
    assert sleep_state.read(fresh, DEPLOYMENT).state == "sleeping"
    assert omgeving.commits == []
    assert omgeving.reprocess_calls == []


@pytest.mark.asyncio
async def test_een_deployment_die_niet_meer_waking_is_wordt_niet_afgerond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De storekopie zegt het al: geen kubectl, geen commit."""
    cached = _project_data({"state": "sleeping", "wake-token": "x"})
    omgeving = _omgeving(monkeypatch, cached=cached, ready=[True])

    result = await flow.finish_wake(PROJECT, DEPLOYMENT)

    assert (result.changed, result.state) == (False, "sleeping")
    assert omgeving.kubectl_calls == []
    assert omgeving.commits == []


# --- 5. De API-route wacht de afronding niet af ---------------------------------------


@pytest.mark.asyncio
async def test_de_api_route_plant_de_afronding_in_na_het_antwoord() -> None:
    from fastapi import BackgroundTasks
    from opi.services.catalog.sleep_mode.router import finish_wake_after_response, wake

    background = BackgroundTasks()
    with (
        patch.object(flow, "wake", AsyncMock(return_value=flow.WakeResult(changed=True, state="waking"))),
        patch.object(flow, "finish_wake", AsyncMock()) as finish,
    ):
        response = await wake(PROJECT, DEPLOYMENT, background, x_wake_token="geheim")

    assert response.status_code == 202
    # De wekkerpod wacht hier niet op: de afronding staat ingepland, niet afgewacht.
    finish.assert_not_called()
    assert [(taak.func, taak.args) for taak in background.tasks] == [
        (finish_wake_after_response, (PROJECT, DEPLOYMENT))
    ]


@pytest.mark.asyncio
async def test_een_noop_plant_geen_afronding_in() -> None:
    from fastapi import BackgroundTasks
    from opi.services.catalog.sleep_mode.router import wake

    background = BackgroundTasks()
    with patch.object(flow, "wake", AsyncMock(return_value=flow.WakeResult(changed=False, state="awake"))):
        response = await wake(PROJECT, DEPLOYMENT, background, x_wake_token="geheim")

    assert response.status_code == 200
    assert background.tasks == []


@pytest.mark.asyncio
async def test_de_achtergrondafronding_laat_een_mislukte_wek_aan_de_sweeper() -> None:
    """Niemand kijkt mee achter de route, dus een time-out wordt gelogd, niet gegooid."""
    from opi.services.catalog.sleep_mode.router import finish_wake_after_response

    with patch.object(flow, "finish_wake", AsyncMock(side_effect=flow.WakeTimeout("kwam niet terug"))):
        await finish_wake_after_response(PROJECT, DEPLOYMENT)


# --- Een schrijfconflict wordt niet ingeslikt, en niet opnieuw geprobeerd ------------


@pytest.mark.asyncio
async def test_een_schrijfconflict_tijdens_afronden_gaat_naar_de_sweeper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``save_and_commit_project`` kan op een gelijktijdige wijziging stuiten.

    Niet opnieuw proberen: de uitzondering gaat omhoog en de sweeper rondt dit af op
    zijn volgende ronde, zoals de PR voorschrijft.
    """
    from opi.services.project_store import ConflictError

    data = _project_data({"state": "waking", "expires-at": (datetime.now(UTC) + timedelta(minutes=9)).isoformat()})
    omgeving = _omgeving(monkeypatch, cached=data, ready=[True])
    omgeving.save.side_effect = ConflictError("concurrent write")

    with pytest.raises(ConflictError):
        await flow.finish_wake(PROJECT, DEPLOYMENT)

    omgeving.save.assert_awaited_once()
    assert omgeving.reprocess_calls == [], "zonder commit is er niets om te reprocessen"


# --- 7. De sweeper gebruikt dezelfde gereedheidsvraag ---------------------------------


@pytest.mark.asyncio
async def test_de_sweeper_vraagt_de_gereedheid_via_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHECK_AWAKE loopt via ``flow.app_is_ready``, niet via een eigen kopie."""
    from opi.services.catalog.sleep_mode.scheduler import CHECK_AWAKE, SleepModeScheduler

    data = _project_data({"state": "waking", "expires-at": (datetime.now(UTC) + timedelta(minutes=9)).isoformat()})
    omgeving = _omgeving(monkeypatch, cached=data, ready=[True])
    gevraagd = AsyncMock(return_value=True)
    monkeypatch.setattr(flow, "app_is_ready", gevraagd)

    gewijzigd = await SleepModeScheduler(CLUSTER)._apply(
        PROJECT, f"{PROJECT}.yaml", [(DEPLOYMENT, CHECK_AWAKE)], datetime.now(UTC)
    )

    assert gewijzigd is True
    gevraagd.assert_awaited_once()
    assert sleep_state.read(data, DEPLOYMENT).state == "awake"
    assert len(omgeving.commits) == 1
