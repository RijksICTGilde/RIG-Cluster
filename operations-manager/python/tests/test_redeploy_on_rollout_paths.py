"""The two rollout paths run the hook, so the reported failure cannot come back (RC-37).

``tests/test_redeploy_hook.py`` holds what the services do; this holds that
``project_manager`` actually asks them. Both are needed: the bug was not that the services
were wrong, it was that ``update_image_and_regenerate`` decided for them with one
hardcoded reason check, and a hook nobody calls fixes nothing.

Each test stops at ``save_and_commit_project`` and inspects the dict that would be
committed -- the same trick as ``test_update_image_saves_to_project_data``, and for the
same reason: everything after it is ArgoCD and manifest work this assertion does not need.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.sleep_mode.state import STATE_AWAKE, STATE_SLEEPING, SleepState, read, write


class _Stop(Exception):
    """Raised from the patched save to end the run at the commit."""


def _project(disabled_reason: str | None = None, sleeping: bool = False) -> dict[str, Any]:
    component: dict[str, Any] = {"reference": "web", "image": "reg/app:broken"}
    if disabled_reason is not None:
        component["disabled"] = True
        component["disabled-reason"] = disabled_reason
    project: dict[str, Any] = {
        "name": "proj",
        "services": [{"sleep-mode": {"config": {"enabled": True, "match": ["preview-*"]}}}],
        "components": [{"name": "web"}],
        "deployments": [
            {
                "name": "preview-42",
                "cluster": settings.CLUSTER_MANAGER,
                "namespace": "proj",
                "components": [component],
            }
        ],
    }
    if sleeping:
        write(project, "preview-42", SleepState(state=STATE_SLEEPING))
    return project


def _manager(project: dict[str, Any], saved: dict[str, Any], *, stop_at_commit: bool = True) -> ProjectManager:
    """A manager whose commit only records what it was given.

    ``stop_at_commit`` raises out of the commit to skip the ArgoCD/manifest tail, which
    the image path runs. The upsert path catches broadly and turns any exception into an
    error result, so there it records and returns instead -- the upsert has no tail to
    skip anyway.
    """
    pm = ProjectManager(project_file_relative_path="projects/proj.yaml")
    pm.get_name = AsyncMock(return_value="proj")
    # A fresh copy per call, like production.
    pm.get_contents = AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(project))

    async def _capture(data: dict, message: str, **kwargs: Any) -> None:
        saved["data"] = data
        if stop_at_commit:
            raise _Stop

    pm.save_and_commit_project = _capture
    return pm


def _component(data: dict[str, Any]) -> dict[str, Any]:
    return data["deployments"][0]["components"][0]


class TestTheImageUpdatePath:
    @pytest.mark.asyncio
    async def test_an_oom_disabled_component_is_switched_back_on(self) -> None:
        """The reported failure, end to end through the manager: before RC-37 the reason
        did not start with an image-pull word, so this component stayed at zero replicas
        and the image update produced a task but no deployment."""
        saved: dict[str, Any] = {}
        pm = _manager(_project(disabled_reason="OOMKilled detected"), saved)

        with pytest.raises(_Stop):
            await pm.update_image_and_regenerate("preview-42", "web", "reg/app:fixed")

        assert _component(saved["data"])["disabled"] is False
        assert "disabled-reason" not in _component(saved["data"])

    @pytest.mark.asyncio
    async def test_a_sleeping_deployment_is_awake_in_the_committed_data(self) -> None:
        saved: dict[str, Any] = {}
        pm = _manager(_project(sleeping=True), saved)

        with pytest.raises(_Stop):
            await pm.update_image_and_regenerate("preview-42", "web", "reg/app:fixed")

        assert read(saved["data"], "preview-42").state == STATE_AWAKE

    @pytest.mark.asyncio
    async def test_the_cleanup_reaches_the_same_dict_as_the_image_change(self) -> None:
        """One commit for the rollout and the cleanup together: if the hook ran on a copy,
        the image would land and the re-enable would not."""
        saved: dict[str, Any] = {}
        pm = _manager(_project(disabled_reason="OOMKilled detected"), saved)

        with pytest.raises(_Stop):
            await pm.update_image_and_regenerate("preview-42", "web", "reg/app:fixed")

        assert _component(saved["data"])["image"] == "reg/app:fixed"
        assert _component(saved["data"])["disabled"] is False


class _ComponentRef:
    """Stand-in for the router's ComponentReference (reference + image)."""

    def __init__(self, reference: str, image: str) -> None:
        self.reference = reference
        self.image = image


class TestTheUpsertPath:
    @pytest.mark.asyncio
    async def test_an_upsert_clears_the_disable_too(self) -> None:
        """The addition the opdrachtgever asked for: an upsert replaces what runs there
        just as an image update does, so it lifts the same state. Naming the hook after
        the action rather than after the image is what let this path join without an
        exception of its own."""
        saved: dict[str, Any] = {}
        project = _project(disabled_reason="OOMKilled detected", sleeping=True)
        pm = _manager(project, saved, stop_at_commit=False)
        pm.get_deployments = AsyncMock(return_value=copy.deepcopy(project["deployments"]))

        result = await pm._upsert_deployment_once("preview-42", [_ComponentRef("web", "reg/app:fixed")])

        assert result["success"] is True
        assert _component(saved["data"])["disabled"] is False
        assert read(saved["data"], "preview-42").state == STATE_AWAKE
        assert result["state_cleared"], "the caller must be able to show what was cleared"

    @pytest.mark.asyncio
    async def test_creating_a_deployment_runs_the_hook_too(self) -> None:
        """A brand-new deployment is a rollout as well, so the services get the moment.

        There is nothing to clear -- nothing ran here yet, so the hook reports nothing --
        but a service that acts on a rollout acts here: sleep-mode starts the sleep clock
        in the commit that creates the deployment, instead of leaving a fresh preview
        without a deadline until the next sweep."""
        saved: dict[str, Any] = {}
        project = _project()
        project["repositories"] = [{"name": "main", "url": "https://example.invalid/app.git"}]
        pm = _manager(project, saved, stop_at_commit=False)
        pm.get_deployments = AsyncMock(return_value=[])

        result = await pm._upsert_deployment_once("preview-99", [_ComponentRef("web", "reg/app:v1")])

        assert result["success"] is True
        assert result["created"] is True
        assert "state_cleared" not in result, "nothing ran here yet, so nothing was cleared"
        new_deployment = next(d for d in saved["data"]["deployments"] if d["name"] == "preview-99")
        assert new_deployment["sleep"]["state"] == STATE_AWAKE
        assert new_deployment["sleep"]["expires-at"]
