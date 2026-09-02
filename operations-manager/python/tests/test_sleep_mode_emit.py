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


async def _emit(project: dict, monkeypatch, *, service_port: int | None = 8080) -> list[str]:
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
        service_port=service_port,
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


@pytest.mark.asyncio
async def test_no_service_port_means_no_waker(monkeypatch) -> None:
    """Without a Service there is nothing for the waker to sit behind.

    Emitting one anyway would put a pod in the namespace that no hostname reaches, which
    is the shape of the bug this port work came from. The deployment still sleeps and is
    still wakeable from the portal or the API.
    """
    project = _project("sleeping")

    assert await _emit(project, monkeypatch, service_port=None) == []


@pytest.mark.asyncio
async def test_the_waker_is_rendered_on_the_service_port(monkeypatch) -> None:
    """End to end through the emit path: the port the caller resolved for the Service is
    the port the waker declares and the port the image is told to listen on."""
    from opi.services.catalog.sleep_mode import manifests as sleep_manifests

    seen: dict[str, int] = {}
    real_deployment = sleep_manifests.build_waker_deployment_values
    real_configmap = sleep_manifests.build_waker_configmap_values

    def spy_deployment(**kwargs):
        seen["deployment"] = kwargs["port"]
        return real_deployment(**kwargs)

    def spy_configmap(**kwargs):
        seen["configmap"] = kwargs["port"]
        return real_configmap(**kwargs)

    monkeypatch.setattr(sleep_manifests, "build_waker_deployment_values", spy_deployment)
    monkeypatch.setattr(sleep_manifests, "build_waker_configmap_values", spy_configmap)

    await _emit(_project("sleeping"), monkeypatch, service_port=8000)

    assert seen == {"deployment": 8000, "configmap": 8000}
