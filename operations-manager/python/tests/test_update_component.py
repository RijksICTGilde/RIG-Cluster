"""Unit tests for ProjectManager.update_component (partial component update).

Mirrors delete_component's read-mutate-save-commit lifecycle: only the provided fields
change, an unknown component is a not_found, and the change is persisted through
save_and_commit_project (the single validated path).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from opi.manager.project_manager import ProjectManager


def _project_with_component() -> dict:
    return {
        "name": "proj",
        "components": [
            {
                "name": "mgr",
                "type": "single",
                "ports": {"inbound": [8443], "outbound": [80, 443]},
                "image": "example.com/mgr:v1",
                "path": [{"match": "/"}],
                "services": [],
            }
        ],
        "services": [],
    }


def _pm(project_data: dict) -> MagicMock:
    pm = MagicMock()
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="proj")
    pm.save_and_commit_project = AsyncMock()
    return pm


async def test_update_ports_replaces_inbound_and_commits() -> None:
    data = _project_with_component()
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", ports=[8443, 9443, 9444])

    assert result["success"] is True
    assert data["components"][0]["ports"]["inbound"] == [8443, 9443, 9444]
    pm.save_and_commit_project.assert_awaited_once()


async def test_update_single_port_alias() -> None:
    data = _project_with_component()
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", port=9443)

    assert result["success"] is True
    assert data["components"][0]["ports"]["inbound"] == [9443]


async def test_update_unknown_component_is_not_found() -> None:
    data = _project_with_component()
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="does-not-exist", ports=[9443])

    assert result["success"] is False
    assert result["error_type"] == "not_found"
    pm.save_and_commit_project.assert_not_called()


async def test_update_only_changes_provided_fields() -> None:
    data = _project_with_component()
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", image="example.com/mgr:v2")

    assert result["success"] is True
    comp = data["components"][0]
    assert comp["image"] == "example.com/mgr:v2"
    # ports left untouched because they were not part of the update
    assert comp["ports"]["inbound"] == [8443]
