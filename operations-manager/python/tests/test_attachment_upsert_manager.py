"""ProjectManager.upsert_attachment: what actually lands in the project file (RC-38).

The endpoint layer decides the verb and the status code; this is the layer that writes.
What matters here is that the catalog entry is encrypted before it is stored, that the id
semantics of the verb are honoured against the *fresh* project data, and that coupling a
component in the same call produces a use and a binding the rest of the platform already
understands -- not a second shape only this path writes.

Git is mocked at ``save_and_commit_project``: the subject is the mutation, and the commit
path has its own tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import extract_attachment_catalog, extract_component_attachment_uses

PUBLIC_KEY = "age1d489e9c48pmwam6603vecp7y29zz9fx5cgpe9uk6cu9l7asfzg9sx5s0tq"


def _manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


def _project(catalog: list | None = None, with_component: bool = True) -> dict:
    services: list = ["publish-on-web"]
    if catalog is not None:
        services.append({"attachments": {"data": catalog}})
    project: dict = {
        "schema-version": 2,
        "name": "demo",
        "services": services,
        "config": {"age-public-key": PUBLIC_KEY},
        "components": [],
    }
    if with_component:
        project["components"] = [{"name": "backend", "type": "single", "services": ["publish-on-web"]}]
    return project


def _wire(project_data: dict):
    """A manager reading this project, with the commit mocked. Returns (manager, save)."""
    manager = _manager()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.get_name = AsyncMock(return_value="demo")
    save = AsyncMock()
    manager.save_and_commit_project = save
    return manager, save


@pytest.fixture(autouse=True)
def _encrypt():
    """AGE encryption stubbed: it shells out, and what matters here is that it is called."""
    with patch(
        "opi.manager.project_manager.encrypt_file_to_age_block_sync",
        side_effect=lambda raw, key: f"-----BEGIN AGE ENCRYPTED FILE-----\n{len(raw)}:{key[:8]}\n",
    ) as encrypt:
        yield encrypt


class TestDefining:
    async def test_it_stores_an_encrypted_entry(self, _encrypt) -> None:
        project = _project(catalog=[])
        manager, save = _wire(project)

        result = await manager.upsert_attachment(
            "server-cert", "server.pem", b"raw-bytes", on_existing="reject", on_absent="create"
        )

        assert result["success"] and result["replaced"] is False
        entry = extract_attachment_catalog(project)["server-cert"]
        assert entry["filename"] == "server.pem"
        assert "BEGIN AGE ENCRYPTED FILE" in entry["content"]
        _encrypt.assert_called_once_with(b"raw-bytes", PUBLIC_KEY)
        save.assert_awaited_once()

    async def test_it_creates_the_catalog_when_the_service_was_never_used(self) -> None:
        project = _project(catalog=None)
        manager, _ = _wire(project)

        result = await manager.upsert_attachment(
            "server-cert", "server.pem", b"raw", on_existing="reject", on_absent="create"
        )

        assert result["success"]
        assert "server-cert" in extract_attachment_catalog(project)

    async def test_a_taken_id_is_refused_when_the_verb_says_so(self) -> None:
        project = _project(catalog=[{"id": "server-cert", "filename": "old.pem", "content": "x"}])
        manager, save = _wire(project)

        result = await manager.upsert_attachment(
            "server-cert", "new.pem", b"raw", on_existing="reject", on_absent="create"
        )

        assert result["error_type"] == "conflict"
        # Refused means nothing changed, not "changed and then reported".
        assert extract_attachment_catalog(project)["server-cert"]["filename"] == "old.pem"
        save.assert_not_awaited()

    async def test_an_absent_id_is_refused_when_the_verb_says_so(self) -> None:
        manager, save = _wire(_project(catalog=[]))

        result = await manager.upsert_attachment("nope", "n.pem", b"raw", on_existing="replace", on_absent="reject")

        assert result["error_type"] == "not_found"
        save.assert_not_awaited()

    async def test_replacing_keeps_one_entry_and_reports_that_it_replaced(self) -> None:
        project = _project(catalog=[{"id": "server-cert", "filename": "old.pem", "content": "x"}])
        manager, _ = _wire(project)

        result = await manager.upsert_attachment(
            "server-cert", "new.pem", b"raw", on_existing="replace", on_absent="create"
        )

        assert result["replaced"] is True
        catalog = extract_attachment_catalog(project)
        assert catalog["server-cert"]["filename"] == "new.pem"
        assert len(project["services"][1]["attachments"]["data"]) == 1

    async def test_a_project_without_an_age_key_is_refused(self) -> None:
        project = _project(catalog=[])
        project["config"] = {}
        manager, save = _wire(project)

        result = await manager.upsert_attachment(
            "server-cert", "s.pem", b"raw", on_existing="reject", on_absent="create"
        )

        assert result["error_type"] == "no_encryption_key"
        save.assert_not_awaited()


class TestDefiningAndBinding:
    async def test_it_records_the_use_and_the_binding_on_the_component(self) -> None:
        project = _project(catalog=[])
        manager, _ = _wire(project)

        result = await manager.upsert_attachment(
            "server-cert",
            "server.pem",
            b"raw",
            on_existing="reject",
            on_absent="create",
            component_name="backend",
            binding={"provide-as": "file", "path": "/etc/ssl/server.pem"},
        )

        assert result["success"]
        component = project["components"][0]
        assert extract_component_attachment_uses(component) == [
            {"reference": "server-cert", "provide-as": "file", "path": "/etc/ssl/server.pem"}
        ]

    async def test_the_service_is_selected_on_project_and_component(self) -> None:
        # A coupling to a service the component does not list resolves to nothing.
        project = _project(catalog=[])
        manager, _ = _wire(project)

        await manager.upsert_attachment(
            "server-cert",
            "server.pem",
            b"raw",
            on_existing="reject",
            on_absent="create",
            component_name="backend",
            binding={"provide-as": "env-var", "env-name": "SERVER_CERT"},
        )

        from opi.services.services import service_entry_name

        assert "attachments" in [service_entry_name(e) for e in project["services"]]
        assert "attachments" in [service_entry_name(e) for e in project["components"][0]["services"]]

    async def test_recoupling_the_same_attachment_replaces_its_binding(self) -> None:
        project = _project(catalog=[{"id": "server-cert", "filename": "s.pem", "content": "x"}])
        project["components"][0]["services"] = [
            "publish-on-web",
            {"attachments": {"config": [{"reference": "server-cert", "provide-as": "file", "path": "/old"}]}},
        ]
        manager, _ = _wire(project)

        await manager.upsert_attachment(
            "server-cert",
            "s.pem",
            b"raw",
            on_existing="replace",
            on_absent="create",
            component_name="backend",
            binding={"provide-as": "file", "path": "/new"},
        )

        uses = extract_component_attachment_uses(project["components"][0])
        assert uses == [{"reference": "server-cert", "provide-as": "file", "path": "/new"}]

    async def test_couplings_of_other_attachments_are_left_alone(self) -> None:
        project = _project(catalog=[{"id": "ca-bundle", "filename": "ca.crt", "content": "x"}])
        project["components"][0]["services"] = [
            {"attachments": {"config": [{"reference": "ca-bundle", "provide-as": "file", "path": "/ca"}]}},
        ]
        manager, _ = _wire(project)

        await manager.upsert_attachment(
            "server-cert",
            "s.pem",
            b"raw",
            on_existing="reject",
            on_absent="create",
            component_name="backend",
            binding={"provide-as": "file", "path": "/new"},
        )

        references = [use["reference"] for use in extract_component_attachment_uses(project["components"][0])]
        assert sorted(references) == ["ca-bundle", "server-cert"]

    async def test_an_unknown_component_is_refused_before_anything_is_written(self) -> None:
        project = _project(catalog=[])
        manager, save = _wire(project)

        result = await manager.upsert_attachment(
            "server-cert",
            "s.pem",
            b"raw",
            on_existing="reject",
            on_absent="create",
            component_name="ghost",
            binding={"provide-as": "file", "path": "/x"},
        )

        assert result["error_type"] == "not_found"
        assert extract_attachment_catalog(project) == {}
        save.assert_not_awaited()


class TestTheWriteGoesThroughTheValidatedPath:
    async def test_a_rejected_save_is_reported_as_a_validation_error(self) -> None:
        from opi.core.project_schema import ProjectIntegrityError

        project = _project(catalog=[])
        manager, save = _wire(project)
        save.side_effect = ProjectIntegrityError("catalogus ongeldig")

        result = await manager.upsert_attachment(
            "server-cert", "s.pem", b"raw", on_existing="reject", on_absent="create"
        )

        assert result["error_type"] == "validation_error"
        assert "catalogus ongeldig" in result["error"]
