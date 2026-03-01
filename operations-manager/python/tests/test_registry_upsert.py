"""Tests for registry upsert methods in ProjectManager."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.project_manager import ProjectManager


def _make_project_manager() -> ProjectManager:
    """Create a ProjectManager instance without calling __init__."""
    pm = ProjectManager.__new__(ProjectManager)
    pm._project_file_handler = MagicMock()
    pm._ProjectManager__has_contents = True
    return pm


@pytest.fixture
def project_manager() -> ProjectManager:
    pm = _make_project_manager()

    mock_git = AsyncMock()
    mock_git.commit_and_push = AsyncMock()

    pm.get_name = AsyncMock(return_value="test-project")
    pm.save_project_data = AsyncMock()
    pm.get_git_connector_for_project_files = AsyncMock(return_value=mock_git)

    return pm


class TestUpsertRegistryBySecret:
    @pytest.mark.asyncio
    async def test_add_new_registry(self, project_manager: ProjectManager) -> None:
        project_data: dict = {"registries": []}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="my-registry", url="rcr.rijksapps.nl/rig", secret_name="rcr-pull-secret"
        )

        assert result["success"] is True
        assert result["created"] is True
        assert result["registry"] == {
            "name": "my-registry",
            "url": "rcr.rijksapps.nl/rig",
            "secretName": "rcr-pull-secret",
        }
        assert len(project_data["registries"]) == 1
        project_manager.save_project_data.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_existing_registry(self, project_manager: ProjectManager) -> None:
        project_data: dict = {
            "registries": [{"name": "my-registry", "url": "old.registry.io", "secretName": "old-secret"}]
        }
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="my-registry", url="rcr.rijksapps.nl/rig", secret_name="new-secret"
        )

        assert result["success"] is True
        assert result["created"] is False
        assert len(project_data["registries"]) == 1
        assert project_data["registries"][0]["secretName"] == "new-secret"
        assert project_data["registries"][0]["url"] == "rcr.rijksapps.nl/rig"

    @pytest.mark.asyncio
    async def test_add_when_no_registries_key(self, project_manager: ProjectManager) -> None:
        project_data: dict = {}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="new-reg", url="ghcr.io", secret_name="ghcr-secret"
        )

        assert result["success"] is True
        assert result["created"] is True
        assert len(project_data["registries"]) == 1

    @pytest.mark.asyncio
    async def test_preserves_other_registries(self, project_manager: ProjectManager) -> None:
        project_data: dict = {"registries": [{"name": "existing", "url": "docker.io", "secretName": "docker-secret"}]}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="new-reg", url="ghcr.io", secret_name="ghcr-secret"
        )

        assert result["success"] is True
        assert len(project_data["registries"]) == 2
        assert project_data["registries"][0]["name"] == "existing"
        assert project_data["registries"][1]["name"] == "new-reg"

    @pytest.mark.asyncio
    async def test_commit_message_add(self, project_manager: ProjectManager) -> None:
        project_data: dict = {}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        await project_manager.upsert_registry_by_secret(name="my-reg", url="ghcr.io", secret_name="secret")

        git_mock = await project_manager.get_git_connector_for_project_files()
        git_mock.commit_and_push.assert_awaited_once_with(
            "Add registry 'my-reg' (secretName) in project 'test-project'"
        )

    @pytest.mark.asyncio
    async def test_commit_message_update(self, project_manager: ProjectManager) -> None:
        project_data: dict = {"registries": [{"name": "my-reg", "url": "old.io", "secretName": "old"}]}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        await project_manager.upsert_registry_by_secret(name="my-reg", url="new.io", secret_name="new")

        git_mock = await project_manager.get_git_connector_for_project_files()
        git_mock.commit_and_push.assert_awaited_once_with(
            "Update registry 'my-reg' (secretName) in project 'test-project'"
        )


class TestUpsertRegistryByCredentials:
    @pytest.mark.asyncio
    @patch("opi.manager.project_manager.encrypt_age_content", new_callable=AsyncMock)
    @patch("opi.manager.project_manager.get_project_public_key", return_value="age1publickey123")
    async def test_add_new_registry(
        self, mock_pubkey: MagicMock, mock_encrypt: AsyncMock, project_manager: ProjectManager
    ) -> None:
        mock_encrypt.return_value = "-----BEGIN AGE ENCRYPTED FILE-----\nencrypted\n-----END AGE ENCRYPTED FILE-----"
        project_data: dict = {"registries": []}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_credentials(
            name="my-registry", url="ghcr.io", username="myuser", password="mytoken"
        )

        assert result["success"] is True
        assert result["created"] is True
        # Response should NOT include encrypted password
        assert "password" not in result["registry"]
        assert result["registry"]["username"] == "myuser"
        # But project_data should have it
        assert project_data["registries"][0]["password"].startswith("-----BEGIN AGE")
        mock_encrypt.assert_awaited_once_with("mytoken", "age1publickey123")

    @pytest.mark.asyncio
    @patch("opi.manager.project_manager.encrypt_age_content", new_callable=AsyncMock)
    @patch("opi.manager.project_manager.get_project_public_key", return_value="age1publickey123")
    async def test_update_existing_registry(
        self, mock_pubkey: MagicMock, mock_encrypt: AsyncMock, project_manager: ProjectManager
    ) -> None:
        mock_encrypt.return_value = "encrypted-new"
        project_data: dict = {
            "registries": [{"name": "my-registry", "url": "ghcr.io", "username": "old", "password": "encrypted-old"}]
        }
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_credentials(
            name="my-registry", url="ghcr.io", username="newuser", password="newpass"
        )

        assert result["success"] is True
        assert result["created"] is False
        assert len(project_data["registries"]) == 1
        assert project_data["registries"][0]["username"] == "newuser"
        assert project_data["registries"][0]["password"] == "encrypted-new"

    @pytest.mark.asyncio
    @patch("opi.manager.project_manager.get_project_public_key", return_value=None)
    async def test_fails_without_public_key(self, mock_pubkey: MagicMock, project_manager: ProjectManager) -> None:
        project_data: dict = {}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_credentials(
            name="my-registry", url="ghcr.io", username="user", password="pass"
        )

        assert result["success"] is False
        assert result["error_type"] == "missing_public_key"
        project_manager.save_project_data.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("opi.manager.project_manager.encrypt_age_content", new_callable=AsyncMock)
    @patch("opi.manager.project_manager.get_project_public_key", return_value="age1publickey123")
    async def test_commit_message_add(
        self, mock_pubkey: MagicMock, mock_encrypt: AsyncMock, project_manager: ProjectManager
    ) -> None:
        mock_encrypt.return_value = "encrypted"
        project_data: dict = {}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        await project_manager.upsert_registry_by_credentials(
            name="my-reg", url="ghcr.io", username="user", password="pass"
        )

        git_mock = await project_manager.get_git_connector_for_project_files()
        git_mock.commit_and_push.assert_awaited_once_with(
            "Add registry 'my-reg' (credentials) in project 'test-project'"
        )
