"""Tests for detail page inline editing: sections, router, and enforcement."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.visualizers.wizard_sections import (
    AUTH_WALL_CONFIG_SECTION,
    EDIT_SECTIONS,
    IDENTITY_EDIT_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    SERVICES_EDIT_SECTION,
    _extract_services,
)
from opi.web.router_detail_edit import (
    _get_edit_section,
    _render_section_html,
)

# ---------------------------------------------------------------------------
# Section definitions
# ---------------------------------------------------------------------------


class TestEditSectionDefinitions:
    def test_identity_edit_section_id(self):
        assert IDENTITY_EDIT_SECTION.section_id == "identity-edit"

    def test_identity_edit_has_only_description(self):
        assert len(IDENTITY_EDIT_SECTION.editables) == 1
        assert IDENTITY_EDIT_SECTION.editables[0].editable.yaml_path == "description"

    def test_identity_edit_post_save_action(self):
        assert IDENTITY_EDIT_SECTION.post_save_action == "save_only"

    def test_services_edit_section_id(self):
        assert SERVICES_EDIT_SECTION.section_id == "services-edit"

    def test_services_edit_has_services(self):
        assert len(SERVICES_EDIT_SECTION.editables) == 1

    def test_services_edit_post_save_action(self):
        assert SERVICES_EDIT_SECTION.post_save_action == "process_project"

    def test_edit_sections_registry_contains_edit_sections(self):
        assert "identity-edit" in EDIT_SECTIONS
        assert "services-edit" in EDIT_SECTIONS

    def test_edit_sections_registry_contains_config_sections(self):
        assert "keycloak-config" in EDIT_SECTIONS
        assert "postgresql-config" in EDIT_SECTIONS
        assert "auth-wall-config" in EDIT_SECTIONS

    def test_edit_sections_registry_count(self):
        assert len(EDIT_SECTIONS) == 5

    def test_edit_sections_reference_same_config_section_objects(self):
        assert EDIT_SECTIONS["keycloak-config"] is KEYCLOAK_CONFIG_SECTION
        assert EDIT_SECTIONS["postgresql-config"] is POSTGRESQL_CONFIG_SECTION
        assert EDIT_SECTIONS["auth-wall-config"] is AUTH_WALL_CONFIG_SECTION


class TestFormSectionPostSaveAction:
    def test_default_is_save_only(self):
        section = FormSection(section_id="test", title="Test")
        assert section.post_save_action == "save_only"

    def test_can_set_process_project(self):
        section = FormSection(section_id="test", title="Test", post_save_action="process_project")
        assert section.post_save_action == "process_project"


# ---------------------------------------------------------------------------
# Router helpers
# ---------------------------------------------------------------------------


class TestGetEditSection:
    def test_returns_known_section(self):
        section = _get_edit_section("identity-edit")
        assert section is IDENTITY_EDIT_SECTION

    def test_raises_404_for_unknown_section(self):
        with pytest.raises(HTTPException) as exc_info:
            _get_edit_section("nonexistent")
        assert exc_info.value.status_code == 404

    def test_returns_config_section(self):
        section = _get_edit_section("keycloak-config")
        assert section is KEYCLOAK_CONFIG_SECTION


class TestRenderSectionHtml:
    def test_renders_section_with_layout(self):
        editable = EditableVisualizer(
            editable=Editable(yaml_path="description"),
            widget=WidgetType.TEXTAREA,
            label="Omschrijving",
        )
        section = FormSection(
            section_id="test-edit",
            title="Test",
            editables=[editable],
            layout=["description"],
        )
        html = _render_section_html(section, yaml_data={"description": "Hello world"})
        assert html
        assert "Hello world" in html or "description" in html

    def test_returns_empty_for_no_layout(self):
        section = FormSection(section_id="test", title="Test", layout=None)
        assert _render_section_html(section, yaml_data={}) == ""

    def test_renders_with_errors(self):
        editable = EditableVisualizer(
            editable=Editable(yaml_path="description"),
            widget=WidgetType.TEXTAREA,
            label="Omschrijving",
        )
        section = FormSection(
            section_id="test-edit",
            title="Test",
            editables=[editable],
            layout=["description"],
        )
        errors = {"description": ["Dit veld is verplicht"]}
        html = _render_section_html(section, yaml_data={}, errors=errors)
        assert html  # Should render even with errors


# ---------------------------------------------------------------------------
# Service add-only enforcement
# ---------------------------------------------------------------------------


class TestServiceAddOnlyEnforcement:
    """Test the service removal detection logic used in the POST handler."""

    def test_extract_services_string_format(self):
        data = {"services": ["keycloak", "redis"]}
        assert set(_extract_services(data)) == {"keycloak", "redis"}

    def test_extract_services_dict_format(self):
        data = {"services": [{"name": "keycloak"}, {"name": "redis"}]}
        assert set(_extract_services(data)) == {"keycloak", "redis"}

    def test_extract_services_mixed_format(self):
        data = {"services": ["keycloak", {"name": "redis"}]}
        assert set(_extract_services(data)) == {"keycloak", "redis"}

    def test_extract_services_empty(self):
        assert _extract_services({}) == []
        assert _extract_services({"services": []}) == []

    def test_detect_removed_services(self):
        existing = {"services": ["keycloak", "redis"]}
        submitted = {"services": ["keycloak"]}
        existing_set = set(_extract_services(existing))
        submitted_set = set(_extract_services(submitted))
        removed = existing_set - submitted_set
        assert removed == {"redis"}

    def test_detect_added_services(self):
        existing = {"services": ["keycloak"]}
        submitted = {"services": ["keycloak", "redis"]}
        existing_set = set(_extract_services(existing))
        submitted_set = set(_extract_services(submitted))
        added = submitted_set - existing_set
        assert added == {"redis"}

    def test_no_changes_detected(self):
        existing = {"services": ["keycloak"]}
        submitted = {"services": ["keycloak"]}
        existing_set = set(_extract_services(existing))
        submitted_set = set(_extract_services(submitted))
        assert existing_set - submitted_set == set()
        assert submitted_set - existing_set == set()


# ---------------------------------------------------------------------------
# Route endpoint tests (mocked)
# ---------------------------------------------------------------------------


class MockProjectInfo:
    """Minimal mock of the ProjectInfo returned by project_service."""

    def __init__(self, name: str, data: dict[str, Any] | None = None, filename: str = "/tmp/test.yaml"):
        self.name = name
        self.data = data or {"name": name, "display-name": name, "description": "test"}
        self.filename = filename


def _mock_project_service(project: MockProjectInfo | None = None, user_role: str = "owner"):
    """Create a mock project service that returns the given project."""
    svc = MagicMock()
    svc.get_project.return_value = project
    svc.is_user_authorized_for_project.return_value = project is not None
    svc.get_user_role_for_project.return_value = user_role
    svc.load_project_from_data = MagicMock()
    return svc


class TestGetEditSectionEndpoint:
    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_project(self):
        svc = _mock_project_service(project=None)
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import get_edit_section

            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await get_edit_section(request, "nonexistent", "identity-edit")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_403_for_unauthorized_user(self):
        project = MockProjectInfo("test-project")
        svc = _mock_project_service(project=project)
        svc.is_user_authorized_for_project.return_value = False
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import get_edit_section

            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await get_edit_section(request, "test-project", "identity-edit")
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_returns_403_for_viewer_role(self):
        project = MockProjectInfo("test-project")
        svc = _mock_project_service(project=project, user_role="viewer")
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import get_edit_section

            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await get_edit_section(request, "test-project", "identity-edit")
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_section(self):
        project = MockProjectInfo("test-project")
        svc = _mock_project_service(project=project)
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import get_edit_section

            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await get_edit_section(request, "test-project", "nonexistent-section")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_html_for_valid_request(self):
        project = MockProjectInfo("test-project", data={"name": "test-project", "description": "My project"})
        svc = _mock_project_service(project=project)
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import get_edit_section

            request = MagicMock()
            response = await get_edit_section(request, "test-project", "identity-edit")
            assert response.status_code == 200
            assert response.body  # non-empty HTML


class TestSubmitEditSectionEndpoint:
    @pytest.mark.asyncio
    async def test_rejects_service_removal(self):
        project_data = {
            "name": "test-project",
            "display-name": "Test",
            "description": "test",
            "services": ["keycloak", "redis"],
        }
        project = MockProjectInfo("test-project", data=project_data)
        svc = _mock_project_service(project=project)

        request = MagicMock()
        request.json = AsyncMock(return_value={"services": ["keycloak"]})

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import submit_edit_section

            response = await submit_edit_section(request, "test-project", "services-edit")
            assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_project(self):
        svc = _mock_project_service(project=None)
        request = MagicMock()

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import submit_edit_section

            with pytest.raises(HTTPException) as exc_info:
                await submit_edit_section(request, "nonexistent", "identity-edit")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_403_for_viewer(self):
        project = MockProjectInfo("test-project")
        svc = _mock_project_service(project=project, user_role="viewer")
        request = MagicMock()

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import submit_edit_section

            with pytest.raises(HTTPException) as exc_info:
                await submit_edit_section(request, "test-project", "identity-edit")
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_successful_edit_triggers_processing(self):
        project_data = {
            "name": "test-project",
            "display-name": "Test",
            "description": "old description",
        }
        project = MockProjectInfo("test-project", data=project_data, filename="/tmp/test-project.yaml")
        svc = _mock_project_service(project=project)

        request = MagicMock()
        request.json = AsyncMock(return_value={"description": "new description"})

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
            patch("opi.handlers.project_file_handler.save_project_file") as mock_save,
            patch("opi.core.task_manager.create_task", return_value="task-123"),
            patch("opi.core.simple_background.process_project_yaml_background"),
        ):
            from opi.web.router_detail_edit import submit_edit_section

            response = await submit_edit_section(request, "test-project", "identity-edit")
            assert response.status_code == 200

            # Verify save was called
            mock_save.assert_called_once()
            saved_data = mock_save.call_args[0][1]
            assert saved_data["description"] == "new description"

            # Verify redirect to progress page
            assert "HX-Redirect" in response.headers
            assert "progress/task-123" in response.headers["HX-Redirect"]

    @pytest.mark.asyncio
    async def test_new_service_triggers_next_section_header(self):
        project_data = {
            "name": "test-project",
            "display-name": "Test",
            "description": "test",
            "services": [],
        }
        project = MockProjectInfo("test-project", data=project_data, filename="/tmp/test-project.yaml")
        svc = _mock_project_service(project=project)

        request = MagicMock()
        request.json = AsyncMock(return_value={"services": ["keycloak"]})

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
            patch("opi.handlers.project_file_handler.save_project_file"),
            patch("opi.core.task_manager.create_task", return_value="task-456"),
            patch("opi.core.simple_background.process_project_yaml_background"),
        ):
            from opi.web.router_detail_edit import submit_edit_section

            response = await submit_edit_section(request, "test-project", "services-edit")
            assert response.status_code == 200
            assert response.headers.get("X-Next-Section") == "keycloak-config"
