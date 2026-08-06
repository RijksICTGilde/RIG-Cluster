"""Regression: an image update must land in the project_data that gets saved.

The bug: update_image_and_regenerate() read the project once into `project_data`
(the dict it saves), but found the deployment via get_deployment_by_name(), which
runs its OWN get_contents() and hands back a SEPARATE copy. Setting comp["image"]
on that copy never reached `project_data`, so the store saw no diff (current==data)
and committed nothing -- the image update was silently a no-op. Confirmed live: 0
store-commits for "Update web image" across every project in the concurrency E2E.

This test reproduces it without a cluster: get_contents() returns a FRESH copy on
every call (exactly like production), so a mutation on the wrong copy is invisible
to the saved dict. Before the fix this assertion fails; after it (find the
deployment inside project_data) it passes.
"""

import copy
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager

OLD_IMAGE = "ghcr.io/example/web:1.0"
NEW_IMAGE = "ghcr.io/example/web:2.0"


def _project() -> dict:
    return {
        "name": "proj",
        "components": [{"name": "web"}],
        "deployments": [
            {
                "name": "productie",
                "cluster": settings.CLUSTER_MANAGER,
                "namespace": "proj",
                "components": [{"reference": "web", "image": OLD_IMAGE}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_image_update_lands_in_the_saved_project_data() -> None:
    pm = ProjectManager(project_file_relative_path="projects/proj.yaml")
    pm.get_name = AsyncMock(return_value="proj")
    # Fresh copy per call, like production -- this is what exposed the bug.
    pm.get_contents = AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(_project()))
    pm._project_file_handler = MagicMock()

    saved: dict = {}

    class _Stop(Exception):
        pass

    async def _capture(data: dict, message: str, **kwargs) -> None:
        # Capture what would be committed, then stop before the heavy
        # process_project()/ArgoCD tail we do not need for this assertion.
        saved["data"] = data
        raise _Stop

    pm.save_and_commit_project = _capture

    with pytest.raises(_Stop):
        await pm.update_image_and_regenerate("productie", "web", NEW_IMAGE)

    comp = saved["data"]["deployments"][0]["components"][0]
    assert comp["image"] == NEW_IMAGE, (
        f"image update did not reach the saved project_data (got {comp['image']!r}); "
        "it was applied to a throwaway copy instead of the dict that is committed"
    )
