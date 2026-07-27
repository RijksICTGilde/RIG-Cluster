"""Integration test for ProjectManager._emit_waker_manifests (gating + file bookkeeping).

Exercises the wiring without git/cluster/crypto: the manifest generator is mocked and the
token decrypt is monkeypatched, so this asserts the gate (state + selected component) and
that the three waker files land in created_files.
"""

from unittest.mock import MagicMock

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_manager import ProjectManager


def _project(state: str | None) -> dict:
    sleep = {"state": state, "wake-token": "enc"} if state else None
    deployment = {"name": "PR-1", "cluster": "local", "namespace": "proj", "components": [{"reference": "frontend"}]}
    if sleep:
        deployment["sleep"] = sleep
    return {
        "name": "proj",
        "services": [{"name": "sleep-mode", "config": {"enabled": True, "match": ["PR-*"]}}],
        "components": [{"name": "frontend", "services": ["publish-on-web"]}],
        "deployments": [deployment],
    }


async def _emit(project: dict, monkeypatch) -> list[str]:
    from opi.services.catalog.sleep_mode import token as sleep_token

    async def fake_decrypt(encrypted: str, project_data: dict) -> str:
        return "plaintoken"

    monkeypatch.setattr(sleep_token, "decrypt", fake_decrypt)

    pm = ProjectManager.__new__(ProjectManager)
    pm._manifest_generator = MagicMock()
    pm._manifest_generator.create_manifest_file.return_value = "/tmp/x.yaml"
    pm._project_file_handler = ProjectFileHandler()

    created_files: list[str] = []
    await pm._emit_waker_manifests(
        project_data=project,
        deployment=project["deployments"][0],
        component_name="frontend",
        component_reference="frontend",
        unique_name="PR-1-frontend",
        namespace="rig-proj",
        cluster="local",
        project_name="proj",
        output_dir="/tmp/out",
        created_files=created_files,
    )
    return created_files


@pytest.mark.asyncio
async def test_emits_three_files_when_sleeping(monkeypatch) -> None:
    created = await _emit(_project("sleeping"), monkeypatch)
    assert any("waker-deployment" in f for f in created)
    assert any("waker-config" in f for f in created)
    assert any(f.endswith("-secret.to-sops.yaml") for f in created)
    assert any("waker-token" in f for f in created)


@pytest.mark.asyncio
async def test_emits_nothing_when_awake(monkeypatch) -> None:
    assert await _emit(_project(None), monkeypatch) == []


@pytest.mark.asyncio
async def test_emits_during_waking(monkeypatch) -> None:
    created = await _emit(_project("waking"), monkeypatch)
    assert len(created) == 3


@pytest.mark.asyncio
async def test_skips_when_no_token(monkeypatch) -> None:
    project = _project("sleeping")
    project["deployments"][0]["sleep"].pop("wake-token")
    assert await _emit(project, monkeypatch) == []
