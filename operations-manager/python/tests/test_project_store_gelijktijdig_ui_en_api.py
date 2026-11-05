"""Een UI-bewerking en een gelijktijdige API-patch op hetzelfde projectbestand.

Dit is de reproductie zonder sandbox van wat
`tests/e2e/test_sandbox_reallife.py::test_ui_env_vars_while_api_patches_same_file` op
het echte cluster meet: twee schrijvers raken hetzelfde projectbestand en beide
wijzigingen moeten blijven staan.

Het verschil met `test_concurrent_save_conflict.py` en `test_task_save_base_version.py`
is de VORM van de UI-schrijver. Een modal-bewerking is geen enkele save:
`_process_and_save_modal_edit` slaat op, en daarna start `_start_deployment` een
create_project-taak die hetzelfde bestand nog een keer schreef. Die tweede save droeg
het formulierresultaat van VOOR de eerste save, op de basisversie van het moment dat het
formulier werd geladen.

`test_task_save_base_version.py` pinde die dubbele save voor een gewijzigd SCALAIR veld,
waar de driewegsmerge er netjes doorheen komt. Deze tests doen het met een component dat
uit een lijst verdwijnt, en daar loopt het stuk: een delta adresseert lijstitems op index,
dus een verwijdering ervoor laat de verificatie van de merge afketsen (de bekende
beperking in `_apply_our_change_to`). De taak kreeg dan een ConflictError over zijn eigen
wijziging, die al stond, en de uitrol stopte daarop.

De git-laag is dezelfde fake als in test_project_store.py, zodat de store en de
taakhandler zelf onder test staan en niet een echte clone.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.project_service import get_project_service
from opi.services.project_store import GitProjectStore
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

from test_project_store import (  # type: ignore[import-not-found]
    RELATIVE_PATH,
    FakeGitConnector,
    FakeRemote,
    _project,
)

PROJECT_NAME = "demo"
COMPONENTS = ["web", "alpha", "beta", "gamma"]
OLD_IMAGE = "registry.example.com/web:1"
NEW_IMAGE = "registry.example.com/web:2"
REMOVED = "beta"


def _reallife_project() -> dict[str, Any]:
    """Vier componenten in een deployment, zoals de reallife-projecten op de sandbox."""
    return _project(
        components=[{"name": name, "type": "single"} for name in COMPONENTS],
        deployments=[
            {
                "name": "productie",
                "cluster": "odcn-production",
                "namespace": "demo",
                "components": [
                    {"reference": name, "image": OLD_IMAGE if name == "web" else "registry.example.com/x:1"}
                    for name in COMPONENTS
                ],
            }
        ],
    )


@pytest.fixture
def store_and_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    get_project_service().clear_all_projects()
    remote = FakeRemote()
    remote.commit("initial", {RELATIVE_PATH: dump_yaml_to_string(_reallife_project())})
    connector = FakeGitConnector(remote, str(tmp_path))
    store = GitProjectStore(working_dir=str(tmp_path))

    async def _get_connector() -> FakeGitConnector:
        return connector

    monkeypatch.setattr(store, "get_connector", _get_connector)
    monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.project_manager.get_project_store", lambda: store)

    # Alles na het schrijven naar git is een andere zorg; de save staat hier onder test.
    async def _processed(self: Any, *args: Any, **kwargs: Any) -> bool:
        return True

    async def _closed(self: Any) -> None:
        return None

    async def _monitored(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ProjectManager, "process_project_from_git", _processed)
    monkeypatch.setattr(ProjectManager, "close", _closed)
    monkeypatch.setattr("opi.core.simple_background._monitor_argocd_and_deployment", _monitored)
    monkeypatch.setattr("opi.core.config.settings.OOM_WATCHER_ENABLED", False)

    return store, remote


def _committed(remote: FakeRemote) -> dict[str, Any]:
    parsed = load_yaml_from_string(remote.files[RELATIVE_PATH])
    assert parsed is not None
    return parsed


def _component_names(data: dict[str, Any]) -> list[str]:
    return [c["name"] for c in data.get("components", [])]


def _web_image(data: dict[str, Any]) -> str | None:
    for deployment in data.get("deployments", []):
        for entry in deployment.get("components", []):
            if entry.get("reference") == "web":
                return entry.get("image")
    return None


def _without_component(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Wat de componenten-modal oplevert: het component weg, ook uit de deployment."""
    result = copy.deepcopy(data)
    result["components"] = [c for c in result.get("components", []) if c.get("name") != name]
    for deployment in result.get("deployments", []):
        deployment["components"] = [e for e in deployment.get("components", []) if e.get("reference") != name]
    return result


def _with_new_image(data: dict[str, Any], image: str) -> dict[str, Any]:
    """Wat de API-patch op de image van 'web' oplevert."""
    result = copy.deepcopy(data)
    for deployment in result.get("deployments", []):
        for entry in deployment.get("components", []):
            if entry.get("reference") == "web":
                entry["image"] = image
    return result


def _make_progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name, subject=None: f"task-{name}"
    progress.add_subtask.side_effect = lambda parent, name, subject=None: f"subtask-{name}"
    return progress


async def _run_task(payload: dict[str, Any]) -> tuple[dict[str, Any], MagicMock]:
    from opi.core.task_handlers_project import handle_create_project

    progress = _make_progress()
    return await handle_create_project(payload, progress), progress


async def _ui_edit_races_api_patch(store: Any, remote: FakeRemote) -> tuple[str, dict[str, Any]]:
    """De volgorde die op de sandbox misgaat, tot net voor de taak.

    1. het formulier wordt geladen: de versie van dat moment wordt vastgelegd
    2. de API-patch leest diezelfde versie
    3. de modal slaat op: component weg
    4. de API-patch slaat op en wordt door de store samengevoegd

    Geeft terug wat de taak meekrijgt: de versie van stap 1 en het formulierresultaat
    van stap 3.
    """
    form_load_version = await store.version_of(RELATIVE_PATH)
    assert form_load_version is not None

    as_rendered = await store.read_path(RELATIVE_PATH)
    assert as_rendered is not None
    api_read = copy.deepcopy(as_rendered)

    edited = _without_component(as_rendered, REMOVED)
    await store.save(PROJECT_NAME, edited, message=f"Update {PROJECT_NAME} (componenten)", actor="ui", base=as_rendered)

    await store.save(
        PROJECT_NAME,
        _with_new_image(api_read, NEW_IMAGE),
        message="Patch image van web",
        actor="api",
        base=api_read,
    )

    landed = _committed(remote)
    assert REMOVED not in _component_names(landed), "de verwijdering uit de UI landde niet"
    assert _web_image(landed) == NEW_IMAGE, "de image-patch van de API landde niet"
    return form_load_version, edited


async def test_de_taak_na_een_modal_bewerking_laat_beide_wijzigingen_staan(store_and_remote: Any) -> None:
    """Wat de modal nu naar de taak stuurt: het bestand staat er al, schrijf het niet opnieuw."""
    store, remote = store_and_remote
    _form_load_version, edited = await _ui_edit_races_api_patch(store, remote)

    result, progress = await _run_task(
        {
            "project_name": PROJECT_NAME,
            "yaml_content": dump_yaml_to_string(edited),
            "project_file_already_saved": True,
        }
    )

    assert result["status"] == "success", result.get("error")
    progress.fail_project.assert_not_called()

    landed = _committed(remote)
    assert REMOVED not in _component_names(landed), "de verwijdering uit de UI is verloren"
    assert _web_image(landed) == NEW_IMAGE, "de image-patch van de API is verloren"


async def test_dezelfde_taak_die_het_bestand_wel_herschrijft_strandt_op_zijn_eigen_wijziging(
    store_and_remote: Any,
) -> None:
    """De reproductie van de fout: de taak schreef het bestand nog een keer.

    Met ``base_version`` in plaats van ``project_file_already_saved`` doet de taak de
    tweede save weer, met het formulierresultaat van voor de eerste save en de
    basisversie van voor het formulier. De store kan die delta niet toepassen op een
    lijst waaruit het component al weg is, en de taak faalt op een wijziging die al
    staat. Het bestand in git blijft goed; de uitrol komt er niet.
    """
    store, remote = store_and_remote
    form_load_version, edited = await _ui_edit_races_api_patch(store, remote)

    result, progress = await _run_task(
        {
            "project_name": PROJECT_NAME,
            "yaml_content": dump_yaml_to_string(edited),
            "base_version": form_load_version,
        }
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "conflict"
    progress.fail_project.assert_called_once_with(result["error"])
