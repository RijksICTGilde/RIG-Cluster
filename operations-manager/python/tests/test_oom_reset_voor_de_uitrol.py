"""Een gebruikersactie wist de OOM-tunestaat VOOR de uitrol, niet erna.

De drie taken die een gebruiker start (aanmaken, upsert, handmatige refresh) beginnen met
een schone lei: een nieuwe uitrol is een nieuwe werklast, dus budget en pod-generatie gaan
op nul. Dat wissen stond aan het EIND van de handler, en daar is het schadelijk: de
sync-wacht in dezelfde handler tunet en legt vast welke pod-generatie hij beantwoordde, en
het wissen erna gooide precies dat weg. De automatische refresh die er direct achteraan
komt las dus een lege staat en vermenigvuldigde de limit opnieuw.

Gemeten in productie op toets-hn7/pr-103: upsert tunet 25Mi -> 139Mi en wist zijn eigen
vastlegging, waarna de refresh 42 seconden later 139Mi -> 278Mi deed en op het plafond van
200Mi strandde, op het bewijs van een pod die bij 25Mi omviel.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services import oom_watcher

PM_PATH = "opi.manager.project_manager.ProjectManager"

_PROJECT = "test-project"
_DEPLOYMENT = "dev"
_COMPONENT = "dev-web"
_OUDE_GENERATIE = "4411bb22c"
_NIEUWE_GENERATIE = "6694c99686"


def _sleutel() -> str:
    return f"{_PROJECT}/{_DEPLOYMENT}/{_COMPONENT}"


@pytest.fixture(autouse=True)
def _schone_generatiestaat():
    oom_watcher._last_tuned_pod_template_hash.clear()
    oom_watcher._oom_tune_attempts.clear()
    yield
    oom_watcher._last_tuned_pod_template_hash.clear()
    oom_watcher._oom_tune_attempts.clear()


def _make_progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name: f"task-{name}"
    progress.add_subtask.side_effect = lambda parent, name, **kwargs: f"subtask-{name}"
    return progress


def _tunende_sync(gezien: dict[str, Any]) -> AsyncMock:
    """Een uitrol die, zoals in productie, tijdens het wachten tunet en dat vastlegt."""

    async def _process(*_args: Any, **_kwargs: Any) -> bool:
        gezien["tijdens"] = dict(oom_watcher._last_tuned_pod_template_hash)
        oom_watcher.record_oom_tune_generation(_PROJECT, _DEPLOYMENT, _COMPONENT, _NIEUWE_GENERATIE)
        return True

    return AsyncMock(side_effect=_process)


def _project_manager(gezien: dict[str, Any]) -> AsyncMock:
    pm = AsyncMock()
    pm.process_project_from_git = _tunende_sync(gezien)
    pm.upsert_deployment = AsyncMock(return_value={"success": True, "created": True})
    pm.get_deployment_results = MagicMock(return_value={})
    pm.get_processing_error = MagicMock(return_value=None)
    pm.get_component_failures = MagicMock(return_value=None)
    pm.close = AsyncMock()
    return pm


@pytest.mark.asyncio
async def test_upsert_wist_voor_de_uitrol_en_bewaart_wat_die_vastlegt() -> None:
    from opi.core.task_handlers_project import handle_upsert_deployment

    oom_watcher._last_tuned_pod_template_hash[_sleutel()] = _OUDE_GENERATIE
    gezien: dict[str, Any] = {}

    with (
        patch(PM_PATH, return_value=_project_manager(gezien)),
        patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
    ):
        result = await handle_upsert_deployment(
            {
                "project_name": _PROJECT,
                "deployment_name": _DEPLOYMENT,
                "components": [{"reference": "web", "image": "web:v1"}],
            },
            _make_progress(),
        )

    assert result["status"] == "success"
    assert gezien["tijdens"] == {}, "de staat van de vorige ronde moet voor de uitrol weg zijn"
    assert oom_watcher._last_tuned_pod_template_hash.get(_sleutel()) == _NIEUWE_GENERATIE, (
        "wat de uitrol zelf vastlegde mag er na de handler niet gewist zijn"
    )


@pytest.mark.asyncio
async def test_create_wist_voor_de_uitrol_en_bewaart_wat_die_vastlegt() -> None:
    from opi.core.task_handlers_project import handle_create_project

    oom_watcher._last_tuned_pod_template_hash[_sleutel()] = _OUDE_GENERATIE
    gezien: dict[str, Any] = {}
    git = AsyncMock(create_or_update_file=AsyncMock(), file_exists=AsyncMock(return_value=False))

    with (
        patch("opi.utils.project_utils.validate_project_name", return_value=True),
        patch("opi.connectors.git.GitConnector", return_value=git),
        patch(PM_PATH, return_value=_project_manager(gezien)),
        patch("opi.core.simple_background._monitor_argocd_and_deployment", new=AsyncMock()),
        patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
    ):
        result = await handle_create_project(
            {
                "project_name": _PROJECT,
                "yaml_content": f"name: {_PROJECT}\ndeployments:\n  - name: {_DEPLOYMENT}\n",
            },
            _make_progress(),
        )

    assert result["status"] == "success"
    assert gezien["tijdens"] == {}
    assert oom_watcher._last_tuned_pod_template_hash.get(_sleutel()) == _NIEUWE_GENERATIE


@pytest.mark.asyncio
async def test_handmatige_refresh_wist_voor_de_uitrol() -> None:
    from opi.core.task_handlers_operations import handle_refresh_deployment

    oom_watcher._last_tuned_pod_template_hash[_sleutel()] = _OUDE_GENERATIE
    gezien: dict[str, Any] = {}
    store = MagicMock(get=MagicMock(return_value=MagicMock(filename=f"{_PROJECT}.yaml")))

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=store),
        patch("opi.manager.project_manager.create_project_manager", return_value=_project_manager(gezien)),
        patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
    ):
        result = await handle_refresh_deployment(
            {"project_name": _PROJECT, "deployment_name": _DEPLOYMENT},
            _make_progress(),
        )

    assert result["status"] == "success"
    assert gezien["tijdens"] == {}
    assert oom_watcher._last_tuned_pod_template_hash.get(_sleutel()) == _NIEUWE_GENERATIE


@pytest.mark.asyncio
async def test_de_automatische_refresh_wist_niets() -> None:
    """De refresh die een tune voor zichzelf in de wachtrij zet is geen gebruikersactie.

    Wiste die wel, dan zou elke escalatieronde zijn eigen rem opheffen.
    """
    from opi.core.task_handlers_operations import handle_refresh_deployment

    oom_watcher._last_tuned_pod_template_hash[_sleutel()] = _OUDE_GENERATIE
    gezien: dict[str, Any] = {}
    store = MagicMock(get=MagicMock(return_value=MagicMock(filename=f"{_PROJECT}.yaml")))

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=store),
        patch("opi.manager.project_manager.create_project_manager", return_value=_project_manager(gezien)),
        patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
    ):
        await handle_refresh_deployment(
            {"project_name": _PROJECT, "deployment_name": _DEPLOYMENT, "automated_remediation": True},
            _make_progress(),
        )

    assert gezien["tijdens"] == {_sleutel(): _OUDE_GENERATIE}, (
        "een automatische refresh moet de vastlegging van de vorige ronde juist zien"
    )
