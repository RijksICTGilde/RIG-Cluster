"""Tests for ProjectManager.patch_service_config_list.

The PATCH counterpart of configure_service: the adapter merges the add/remove delta on
the freshly read project file, and the change persists through the single validated
save path like every other write.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from opi.manager.project_manager import ProjectManager


def _project() -> dict:
    return {
        "name": "proj",
        "services": ["attachments"],
        "components": [
            {
                "name": "backend",
                "type": "single",
                "services": [
                    {
                        "reference": "persistent-storage",
                        "config": [
                            {"name": "data1", "size": "1Gi", "mount-path": "/data1"},
                            {"name": "data2", "size": "1Gi", "mount-path": "/data2"},
                        ],
                    }
                ],
            }
        ],
    }


def _pm(project_data: dict) -> MagicMock:
    pm = MagicMock()
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="proj")
    pm.save_and_commit_project = AsyncMock()
    return pm


async def test_patch_commits_and_reports_counts() -> None:
    data = _project()
    pm = _pm(data)

    result = await ProjectManager.patch_service_config_list(
        pm,
        "persistent-storage",
        "component",
        add=[],
        remove=["data2"],
        component_name="backend",
    )

    assert result["success"] is True
    assert result["added"] == 0
    assert result["removed"] == 1
    pm.save_and_commit_project.assert_awaited_once()
    entry = data["components"][0]["services"][0]
    assert entry["config"] == [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}]


async def test_patch_leaves_everything_else_alone() -> None:
    data = _project()
    pm = _pm(data)

    result = await ProjectManager.patch_service_config_list(
        pm,
        "persistent-storage",
        "component",
        add=[{"name": "data3", "size": "1Gi", "mount-path": "/data3"}],
        remove=[],
        component_name="backend",
    )

    assert result["success"] is True
    assert result["added"] == 1
    # the untouched entries keep their values verbatim
    assert [entry["name"] for entry in data["components"][0]["services"][0]["config"]] == [
        "data1",
        "data2",
        "data3",
    ]


async def test_patch_an_invalid_entry_never_commits() -> None:
    data = _project()
    pm = _pm(data)

    result = await ProjectManager.patch_service_config_list(
        pm,
        "persistent-storage",
        "component",
        add=[{"name": "broken", "size": "1Gi"}],  # mount-path missing
        remove=[],
        component_name="backend",
    )

    assert result["success"] is False
    assert result["error_type"] == "invalid_target"
    pm.save_and_commit_project.assert_not_called()


async def test_patch_unknown_target_layer_is_refused() -> None:
    data = _project()
    pm = _pm(data)

    result = await ProjectManager.patch_service_config_list(
        pm,
        "persistent-storage",
        "not-a-layer",
        add=[],
        remove=["data2"],
        component_name="backend",
    )

    assert result["success"] is False
    assert result["error_type"] == "invalid_target"
    pm.save_and_commit_project.assert_not_called()
