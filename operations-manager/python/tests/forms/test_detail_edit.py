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
    COMPONENTS_EDIT_SECTION,
    COMPONENTS_SECTION,
    EDIT_SECTIONS,
    IDENTITY_EDIT_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    SERVICES_EDIT_SECTION,
    TEAM_SECTION,
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

    def test_identity_edit_has_display_name_and_description(self):
        assert len(IDENTITY_EDIT_SECTION.editables) == 2
        paths = [e.editable.yaml_path for e in IDENTITY_EDIT_SECTION.editables]
        assert "display-name" in paths
        assert "description" in paths

    def test_identity_edit_post_save_action(self):
        assert IDENTITY_EDIT_SECTION.post_save_action == "save_only"

    def test_services_edit_section_id(self):
        assert SERVICES_EDIT_SECTION.section_id == "services-edit"

    def test_services_edit_has_services(self):
        assert len(SERVICES_EDIT_SECTION.editables) == 1

    def test_services_edit_post_save_action(self):
        assert SERVICES_EDIT_SECTION.post_save_action == "process_project"

    def test_components_edit_section_id(self):
        assert COMPONENTS_EDIT_SECTION.section_id == "components-edit"

    def test_components_edit_post_save_action(self):
        assert COMPONENTS_EDIT_SECTION.post_save_action == "process_project"

    def test_components_edit_reuses_components_section_editables(self):
        assert COMPONENTS_EDIT_SECTION.editables is COMPONENTS_SECTION.editables

    def test_components_edit_reuses_components_section_layout(self):
        assert COMPONENTS_EDIT_SECTION.layout is COMPONENTS_SECTION.layout

    def test_team_edit_is_save_only(self):
        """Team section uses default post_save_action (save_only)."""
        assert TEAM_SECTION.post_save_action == "save_only"

    def test_edit_sections_registry_contains_edit_sections(self):
        assert "identity-edit" in EDIT_SECTIONS
        assert "services-edit" in EDIT_SECTIONS

    def test_edit_sections_registry_contains_team_and_components(self):
        assert "team-edit" in EDIT_SECTIONS
        assert "components-edit" in EDIT_SECTIONS

    def test_team_edit_reuses_team_section(self):
        assert EDIT_SECTIONS["team-edit"] is TEAM_SECTION

    def test_components_edit_uses_dedicated_section(self):
        assert EDIT_SECTIONS["components-edit"] is COMPONENTS_EDIT_SECTION

    def test_edit_sections_registry_contains_config_sections(self):
        assert "keycloak-config" in EDIT_SECTIONS
        assert "postgresql-config" in EDIT_SECTIONS
        assert "auth-wall-config" in EDIT_SECTIONS

    def test_edit_sections_registry_count(self):
        assert len(EDIT_SECTIONS) == 7

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

    def test_returns_team_edit_section(self):
        section = _get_edit_section("team-edit")
        assert section is TEAM_SECTION

    def test_returns_components_edit_section(self):
        section = _get_edit_section("components-edit")
        assert section is COMPONENTS_EDIT_SECTION


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

    def test_renders_team_edit_section(self):
        yaml_data = {
            "users": [
                {"email": "alice@example.com", "role": "owner"},
                {"email": "bob@example.com", "role": "member"},
            ],
        }
        html = _render_section_html(TEAM_SECTION, yaml_data=yaml_data)
        assert html
        assert "<c-" not in html, f"Unprocessed component tags in team-edit HTML: {html[:500]}"

    def test_renders_components_edit_section(self):
        yaml_data = {
            "components": [
                {"name": "frontend", "image": "nginx:latest"},
            ],
            "services": [],
        }
        html = _render_section_html(COMPONENTS_EDIT_SECTION, yaml_data=yaml_data)
        assert html
        assert "<c-" not in html, f"Unprocessed component tags in components-edit HTML: {html[:500]}"

    def test_renders_identity_edit_section_no_unprocessed_tags(self):
        yaml_data = {"display-name": "My Project", "description": "A test project"}
        html = _render_section_html(IDENTITY_EDIT_SECTION, yaml_data=yaml_data)
        assert html
        assert "<c-" not in html, f"Unprocessed component tags in identity-edit HTML: {html[:500]}"

    def test_identity_edit_renders_both_fields(self):
        yaml_data = {"display-name": "My Project", "description": "A test project"}
        html = _render_section_html(IDENTITY_EDIT_SECTION, yaml_data=yaml_data)
        # Both field inputs should be present in the rendered output
        assert 'name="display-name"' in html
        assert 'name="description"' in html
        # Display name value should appear in the text input
        assert "My Project" in html


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

    @pytest.mark.asyncio
    async def test_returns_404_for_invisible_config_section(self):
        """Keycloak-config should return 404 when project has no keycloak service."""
        project_data = {"name": "test-project", "description": "test", "services": []}
        project = MockProjectInfo("test-project", data=project_data)
        svc = _mock_project_service(project=project)
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import get_edit_section

            request = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await get_edit_section(request, "test-project", "keycloak-config")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_html_for_visible_config_section(self):
        """Keycloak-config should render when project has keycloak service."""
        project_data = {"name": "test-project", "description": "test", "services": ["keycloak"]}
        project = MockProjectInfo("test-project", data=project_data)
        svc = _mock_project_service(project=project)
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import get_edit_section

            request = MagicMock()
            response = await get_edit_section(request, "test-project", "keycloak-config")
            assert response.status_code == 200
            assert response.body


class TestSubmitEditSectionVisibility:
    """Verify POST endpoint also checks section visibility."""

    @pytest.mark.asyncio
    async def test_post_returns_404_for_invisible_section(self):
        """Cannot submit keycloak-config when project has no keycloak service."""
        project_data = {"name": "test-project", "description": "test", "services": []}
        project = MockProjectInfo("test-project", data=project_data)
        svc = _mock_project_service(project=project)
        request = MagicMock()
        request.json = AsyncMock(return_value={})
        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import submit_edit_section

            with pytest.raises(HTTPException) as exc_info:
                await submit_edit_section(request, "test-project", "keycloak-config")
            assert exc_info.value.status_code == 404


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
    async def test_save_only_section_does_not_trigger_processing(self):
        """Identity-edit is save_only: should save but NOT create a background task."""
        project_data = {
            "name": "test-project",
            "display-name": "Test",
            "description": "old description",
        }
        project = MockProjectInfo("test-project", data=project_data, filename="/tmp/test-project.yaml")
        svc = _mock_project_service(project=project)

        request = MagicMock()
        request.json = AsyncMock(return_value={"display-name": "Test", "description": "new description"})

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
            patch("opi.handlers.project_file_handler.save_project_file") as mock_save,
            patch("opi.core.task_manager.create_task") as mock_create_task,
        ):
            from opi.web.router_detail_edit import submit_edit_section

            response = await submit_edit_section(request, "test-project", "identity-edit")
            assert response.status_code == 200

            # Save was called
            mock_save.assert_called_once()
            saved_data = mock_save.call_args[0][1]
            assert saved_data["description"] == "new description"

            # No background task was created
            mock_create_task.assert_not_called()

            # No task-related headers
            assert "X-Task-Id" not in response.headers
            assert "HX-Redirect" not in response.headers

    @pytest.mark.asyncio
    async def test_process_project_section_triggers_background_task(self):
        """Services-edit is process_project: should create task and return X-Task-Id."""
        project_data = {
            "name": "test-project",
            "display-name": "Test",
            "description": "test",
            "services": ["keycloak"],
        }
        project = MockProjectInfo("test-project", data=project_data, filename="/tmp/test-project.yaml")
        svc = _mock_project_service(project=project)

        request = MagicMock()
        request.json = AsyncMock(return_value={"services": ["keycloak", "redis"]})

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
            patch("opi.handlers.project_file_handler.save_project_file"),
            patch("opi.core.task_manager.create_task", return_value="task-789"),
            patch("opi.core.simple_background.process_project_yaml_background"),
        ):
            from opi.web.router_detail_edit import submit_edit_section

            response = await submit_edit_section(request, "test-project", "services-edit")
            assert response.status_code == 200

            # Background task was created
            assert response.headers.get("X-Task-Id") == "task-789"

            # No HX-Redirect (we show progress in modal now)
            assert "HX-Redirect" not in response.headers

    @pytest.mark.asyncio
    async def test_team_edit_saves_users(self):
        """Team edit should save user changes (save_only, no background task)."""
        project_data = {
            "name": "test-project",
            "display-name": "Test",
            "description": "test",
            "users": [{"email": "alice@example.com", "role": "owner"}],
        }
        project = MockProjectInfo("test-project", data=project_data, filename="/tmp/test-project.yaml")
        svc = _mock_project_service(project=project)

        # Simulate nested JSON that collectFormData now produces
        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "users": [
                    {"email": "alice@example.com", "role": "owner"},
                    {"email": "bob@example.com", "role": "administrator"},
                ],
            }
        )

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
            patch("opi.handlers.project_file_handler.save_project_file") as mock_save,
            patch("opi.core.task_manager.create_task") as mock_create_task,
        ):
            from opi.web.router_detail_edit import submit_edit_section

            response = await submit_edit_section(request, "test-project", "team-edit")
            assert response.status_code == 200

            # Save was called with updated users
            mock_save.assert_called_once()
            saved_data = mock_save.call_args[0][1]
            assert len(saved_data["users"]) == 2
            assert saved_data["users"][1]["email"] == "bob@example.com"

            # Team is save_only — no background task
            mock_create_task.assert_not_called()
            assert "X-Task-Id" not in response.headers

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


class TestSequenceActionEndpoint:
    @pytest.mark.asyncio
    async def test_sequence_add_returns_html(self):
        """Sequence add action re-renders the section with an extra item."""
        project_data = {
            "name": "test-project",
            "display-name": "Test",
            "description": "test",
            "users": [{"email": "a@b.nl", "role": "owner"}],
        }
        project = MockProjectInfo("test-project", data=project_data)
        svc = _mock_project_service(project=project)

        request = MagicMock()
        request.json = AsyncMock(return_value={
            "users": [{"email": "a@b.nl", "role": "owner"}],
            "_seq_action": "add",
            "_seq_path": "users",
            "_seq_index": "",
        })

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import sequence_action

            response = await sequence_action(request, "test-project", "team-edit")
            assert response.status_code == 200
            assert len(response.body) > 0

    @pytest.mark.asyncio
    async def test_sequence_action_rejects_invalid_action(self):
        """Invalid action returns 400."""
        project_data = {"name": "test-project", "display-name": "Test", "description": "test"}
        project = MockProjectInfo("test-project", data=project_data)
        svc = _mock_project_service(project=project)

        request = MagicMock()
        request.json = AsyncMock(return_value={
            "_seq_action": "invalid",
            "_seq_path": "users",
            "_seq_index": "",
        })

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import sequence_action

            with pytest.raises(HTTPException) as exc_info:
                await sequence_action(request, "test-project", "team-edit")
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_sequence_action_requires_auth(self):
        """Unauthorized user gets 403."""
        project_data = {"name": "test-project", "display-name": "Test", "description": "test"}
        project = MockProjectInfo("test-project", data=project_data)
        svc = _mock_project_service(project=project, user_role="viewer")

        request = MagicMock()
        request.json = AsyncMock(return_value={
            "_seq_action": "add",
            "_seq_path": "users",
            "_seq_index": "",
        })

        with (
            patch("opi.web.router_detail_edit.get_current_user", return_value={"email": "a@b.nl"}),
            patch("opi.services.project_service.get_project_service", return_value=svc),
        ):
            from opi.web.router_detail_edit import sequence_action

            with pytest.raises(HTTPException) as exc_info:
                await sequence_action(request, "test-project", "team-edit")
            assert exc_info.value.status_code == 403
