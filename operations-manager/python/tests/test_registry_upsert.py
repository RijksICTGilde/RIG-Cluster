"""Tests for registry upsert methods in ProjectManager.

Sinds schemaversie 2.9 (RC-177) staat de lijst niet meer op de projectwortel maar in de
config van de dienst image-registries, en heet het veld ``upstream`` in plaats van ``url``.
De registries worden hier dus gelezen zoals de rest van de code ze leest, via
``project_registries``, en niet via een sleutel op de wortel.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.image_registries.resolution import project_registries


def _registries(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    """De registries zoals ze in het projectbestand terechtkomen."""
    return project_registries(project_data)


def _with_registries(*entries: dict[str, Any]) -> dict[str, Any]:
    """Een projectdict met deze registries in de dienstconfig."""
    return {"name": "test-project", "services": [{"name": "image-registries", "config": {"registries": list(entries)}}]}


def _make_project_manager() -> ProjectManager:
    """Create a ProjectManager instance without calling __init__."""
    pm = ProjectManager.__new__(ProjectManager)
    pm._project_file_handler = MagicMock()
    pm._ProjectManager__has_contents = True
    return pm


@pytest.fixture
def project_manager() -> ProjectManager:
    pm = _make_project_manager()

    pm.get_name = AsyncMock(return_value="test-project")
    pm.save_and_commit_project = AsyncMock()

    return pm


class TestUpsertRegistryBySecret:
    @pytest.mark.asyncio
    async def test_add_new_registry(self, project_manager: ProjectManager) -> None:
        project_data = _with_registries()
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="my-registry", url="rcr.rijksapps.nl/rig", secret_name="rcr-pull-secret"
        )

        assert result["success"] is True
        assert result["created"] is True
        assert result["registry"] == {
            "name": "my-registry",
            "upstream": "rcr.rijksapps.nl/rig",
            "secretName": "rcr-pull-secret",
        }
        assert len(_registries(project_data)) == 1
        project_manager.save_and_commit_project.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_existing_registry(self, project_manager: ProjectManager) -> None:
        project_data = _with_registries(
            {"name": "my-registry", "upstream": "old.registry.io", "secretName": "old-secret"}
        )
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="my-registry", url="rcr.rijksapps.nl/rig", secret_name="new-secret"
        )

        assert result["success"] is True
        assert result["created"] is False
        assert len(_registries(project_data)) == 1
        assert _registries(project_data)[0]["secretName"] == "new-secret"
        assert _registries(project_data)[0]["upstream"] == "rcr.rijksapps.nl/rig"

    @pytest.mark.asyncio
    async def test_add_when_the_service_is_not_there_yet(self, project_manager: ProjectManager) -> None:
        """Een project dat de dienst nog niet draagt krijgt hem erbij, met de registry erin."""
        project_data: dict = {"name": "test-project"}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="new-reg", url="ghcr.io", secret_name="ghcr-secret"
        )

        assert result["success"] is True
        assert result["created"] is True
        assert len(_registries(project_data)) == 1

    @pytest.mark.asyncio
    async def test_preserves_other_registries(self, project_manager: ProjectManager) -> None:
        project_data = _with_registries({"name": "existing", "upstream": "docker.io", "secretName": "docker-secret"})
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_secret(
            name="new-reg", url="ghcr.io", secret_name="ghcr-secret"
        )

        assert result["success"] is True
        assert [r["name"] for r in _registries(project_data)] == ["existing", "new-reg"]

    @pytest.mark.asyncio
    async def test_commit_message_add(self, project_manager: ProjectManager) -> None:
        project_data: dict = {"name": "test-project"}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        await project_manager.upsert_registry_by_secret(name="my-reg", url="ghcr.io", secret_name="secret")

        project_manager.save_and_commit_project.assert_awaited_once()
        assert (
            project_manager.save_and_commit_project.await_args.args[1]
            == "Add registry 'my-reg' (secretName) in project 'test-project'"
        )

    @pytest.mark.asyncio
    async def test_commit_message_update(self, project_manager: ProjectManager) -> None:
        project_data = _with_registries({"name": "my-reg", "upstream": "old.io", "secretName": "old"})
        project_manager.get_contents = AsyncMock(return_value=project_data)

        await project_manager.upsert_registry_by_secret(name="my-reg", url="new.io", secret_name="new")

        project_manager.save_and_commit_project.assert_awaited_once()
        assert (
            project_manager.save_and_commit_project.await_args.args[1]
            == "Update registry 'my-reg' (secretName) in project 'test-project'"
        )


class TestUpsertRegistryByCredentials:
    @pytest.mark.asyncio
    @patch("opi.manager.project_manager.encrypt_age_content", new_callable=AsyncMock)
    @patch("opi.manager.project_manager.get_project_public_key", return_value="age1publickey123")
    async def test_add_new_registry(
        self, mock_pubkey: MagicMock, mock_encrypt: AsyncMock, project_manager: ProjectManager
    ) -> None:
        mock_encrypt.return_value = "-----BEGIN AGE ENCRYPTED FILE-----\nencrypted\n-----END AGE ENCRYPTED FILE-----"
        project_data = _with_registries()
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
        assert _registries(project_data)[0]["password"].startswith("-----BEGIN AGE")
        mock_encrypt.assert_awaited_once_with("mytoken", "age1publickey123")

    @pytest.mark.asyncio
    @patch("opi.manager.project_manager.encrypt_age_content", new_callable=AsyncMock)
    @patch("opi.manager.project_manager.get_project_public_key", return_value="age1publickey123")
    async def test_update_existing_registry(
        self, mock_pubkey: MagicMock, mock_encrypt: AsyncMock, project_manager: ProjectManager
    ) -> None:
        mock_encrypt.return_value = "encrypted-new"
        project_data = _with_registries(
            {"name": "my-registry", "upstream": "ghcr.io", "username": "old", "password": "encrypted-old"}
        )
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_credentials(
            name="my-registry", url="ghcr.io", username="newuser", password="newpass"
        )

        assert result["success"] is True
        assert result["created"] is False
        assert len(_registries(project_data)) == 1
        assert _registries(project_data)[0]["username"] == "newuser"
        assert _registries(project_data)[0]["password"] == "encrypted-new"

    @pytest.mark.asyncio
    @patch("opi.manager.project_manager.get_project_public_key", return_value=None)
    async def test_fails_without_public_key(self, mock_pubkey: MagicMock, project_manager: ProjectManager) -> None:
        project_data: dict = {"name": "test-project"}
        project_manager.get_contents = AsyncMock(return_value=project_data)

        result = await project_manager.upsert_registry_by_credentials(
            name="my-registry", url="ghcr.io", username="user", password="pass"
        )

        assert result["success"] is False
        assert result["error_type"] == "missing_public_key"
        project_manager.save_and_commit_project.assert_not_awaited()

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

        project_manager.save_and_commit_project.assert_awaited_once()
        assert (
            project_manager.save_and_commit_project.await_args.args[1]
            == "Add registry 'my-reg' (credentials) in project 'test-project'"
        )
