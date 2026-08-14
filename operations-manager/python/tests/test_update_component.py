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


async def test_update_port_only_changes_first_port() -> None:
    data = _project_with_component()
    data["components"][0]["ports"]["inbound"] = [8443, 9443, 9444]
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", port=8080)

    assert result["success"] is True
    # the single-port `port` only touches the primary port and keeps the extras
    assert data["components"][0]["ports"]["inbound"] == [8080, 9443, 9444]


async def test_update_ports_empty_clears_inbound() -> None:
    data = _project_with_component()
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", ports=[])

    assert result["success"] is True
    # an explicit empty array clears the ports; no default fallback
    assert data["components"][0]["ports"]["inbound"] == []


async def test_update_rewrite_keeps_the_match() -> None:
    """Updating only the rewrite leaves the path's match as it was."""
    data = _project_with_component()
    data["components"][0]["path"] = [{"match": "/api"}]
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", rewrite="/")

    assert result["success"] is True
    assert data["components"][0]["path"] == [{"match": "/api", "rewrite": "/"}]


async def test_update_path_keeps_the_rewrite() -> None:
    """And the other way round: a new match does not drop the rewrite."""
    data = _project_with_component()
    data["components"][0]["path"] = [{"match": "/api", "rewrite": "/"}]
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", path="/v2")

    assert result["success"] is True
    assert data["components"][0]["path"] == [{"match": "/v2", "rewrite": "/"}]


async def test_update_rewrite_on_a_string_path() -> None:
    """Pre-migration files store a bare string; it becomes an entry, keeping the match."""
    data = _project_with_component()
    data["components"][0]["path"] = "/api"
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", rewrite="/")

    assert result["success"] is True
    assert data["components"][0]["path"] == [{"match": "/api", "rewrite": "/"}]


async def test_update_without_path_or_rewrite_leaves_the_path_alone() -> None:
    data = _project_with_component()
    data["components"][0]["path"] = [{"match": "/api", "rewrite": "/"}]
    pm = _pm(data)

    result = await ProjectManager.update_component(pm, name="mgr", image="example.com/mgr:v2")

    assert result["success"] is True
    assert data["components"][0]["path"] == [{"match": "/api", "rewrite": "/"}]


def test_request_rejects_port_and_ports_together() -> None:
    import pytest
    from opi.api.router import AddComponentRequest, UpdateComponentRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UpdateComponentRequest(port=8443, ports=[8443, 9443])
    with pytest.raises(ValidationError):
        AddComponentRequest(name="x", image="img", deployment_names=["d"], port=8443, ports=[8443])


class TestUpdateComponentServices:
    """Replacing a component's services list must not wipe per-service config (RC: vraag 17).

    The PATCH body carries bare names, so config can never travel through it; a service
    that stays selected has to keep the entry it had, attachment couplings and storage
    mounts included. add_services/remove_services adjust the list without naming the rest.
    """

    def _project_with_configured_services(self) -> dict:
        return {
            "name": "proj",
            "components": [
                {
                    "name": "mgr",
                    "type": "single",
                    "image": "example.com/mgr:v1",
                    "services": [
                        {
                            "reference": "attachments",
                            "config": [{"reference": "rapport", "provide-as": "file", "path": "/files"}],
                        },
                        {"reference": "persistent-storage", "config": [{"mountPath": "/data", "size": "2Gi"}]},
                    ],
                }
            ],
            "services": ["attachments", "persistent-storage"],
        }

    async def test_replace_keeps_entries_of_services_that_stay(self) -> None:
        data = self._project_with_configured_services()
        attachment_entry, storage_entry = data["components"][0]["services"]
        pm = _pm(data)

        result = await ProjectManager.update_component(
            pm, name="mgr", services=["attachments", "persistent-storage", "redis"]
        )

        assert result["success"] is True
        assert data["components"][0]["services"] == [attachment_entry, storage_entry, "redis"]

    async def test_replace_drops_services_left_out_with_their_config(self) -> None:
        data = self._project_with_configured_services()
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", services=["redis"])

        assert result["success"] is True
        assert data["components"][0]["services"] == ["redis"]
        # project-level selection is not the PATCH component's business
        assert data["services"] == ["attachments", "persistent-storage", "redis"]

    async def test_add_services_appends_and_leaves_the_rest_alone(self) -> None:
        data = self._project_with_configured_services()
        attachment_entry, storage_entry = data["components"][0]["services"]
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", add_services=["redis"])

        assert result["success"] is True
        assert data["components"][0]["services"] == [attachment_entry, storage_entry, "redis"]

    async def test_add_services_skips_a_service_that_is_already_there(self) -> None:
        data = self._project_with_configured_services()
        services_before = [entry.copy() for entry in data["components"][0]["services"]]
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", add_services=["attachments"])

        assert result["success"] is True
        # not replaced by a bare string: the couplings survive
        assert data["components"][0]["services"] == services_before

    async def test_remove_services_removes_only_that_service_from_that_component(self) -> None:
        data = self._project_with_configured_services()
        _, storage_entry = data["components"][0]["services"]
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", remove_services=["attachments"])

        assert result["success"] is True
        assert data["components"][0]["services"] == [storage_entry]
        # the project-level selection stays; another component may still use it
        assert data["services"] == ["attachments", "persistent-storage"]

    async def test_remove_and_add_together_apply_remove_then_add(self) -> None:
        data = self._project_with_configured_services()
        _, storage_entry = data["components"][0]["services"]
        pm = _pm(data)

        result = await ProjectManager.update_component(
            pm, name="mgr", remove_services=["attachments"], add_services=["redis"]
        )

        assert result["success"] is True
        assert data["components"][0]["services"] == [storage_entry, "redis"]

    async def test_remove_an_unknown_service_name_fails(self) -> None:
        data = self._project_with_configured_services()
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", remove_services=["not-a-service"])

        assert result["success"] is False
        assert result["error_type"] == "invalid_services"
        pm.save_and_commit_project.assert_not_called()

    async def test_add_a_service_that_needs_a_project_decision_fails(self) -> None:
        data = self._project_with_configured_services()
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", add_services=["keycloak"])

        assert result["success"] is False
        assert result["error_type"] == "invalid_services"
        assert "keycloak" in result["error"]
        pm.save_and_commit_project.assert_not_called()


def test_request_rejects_services_with_add_or_remove() -> None:
    import pytest
    from opi.api.router import UpdateComponentRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UpdateComponentRequest(services=["redis"], add_services=["minio-storage"])
    with pytest.raises(ValidationError):
        UpdateComponentRequest(services=["redis"], remove_services=["attachments"])
    with pytest.raises(ValidationError):
        UpdateComponentRequest(add_services=["redis"], remove_services=["redis"])
    # each form on its own is fine
    UpdateComponentRequest(services=["redis"])
    UpdateComponentRequest(add_services=["redis"], remove_services=["attachments"])
