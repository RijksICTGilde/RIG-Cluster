"""Tenant isolation on the shared container registry (RC-98).

The image-push endpoint writes into ONE registry repository, because Quay has no
nested repos under a single robot-account scope. Ownership therefore lives in the
tag, and these tests prove it holds from the outside: two projects, two real API
keys, the same ``image_name`` and ``tag``, and two different destinations.

They deliberately drive the real ``SkopeoConnector`` (only the subprocess and the
availability check are mocked) rather than a stubbed one, so the destination that is
asserted is the destination skopeo would be handed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.image_router import image_router
from opi.connectors.skopeo import SkopeoConnector
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_platform_registry_image_ownership, validate_project_structure

PROJECT_A = "project-a"
PROJECT_B = "project-b"
KEY_A = "key-of-project-a"
KEY_B = "key-of-project-b"

REGISTRY_URL = "rcr.rijksapps.nl"
REGISTRY_ORG = "rig/zad"
PLATFORM_REPO = f"{REGISTRY_URL}/{REGISTRY_ORG}"


@pytest.fixture(autouse=True)
def _reset_skopeo_singleton():
    SkopeoConnector._instance = None
    yield
    SkopeoConnector._instance = None


def _project(name: str, api_key: str) -> MagicMock:
    project = MagicMock()
    project.name = name
    project.api_key = api_key
    return project


@pytest.fixture
def two_projects():
    """A project store holding two projects, each with its own API key."""
    projects = {PROJECT_A: _project(PROJECT_A, KEY_A), PROJECT_B: _project(PROJECT_B, KEY_B)}
    store = MagicMock()
    store.get.side_effect = projects.get
    with patch("opi.api.endpoint_util.get_project_store", return_value=store):
        yield store


@pytest.fixture
def push_destinations():
    """Run the real connector against a mocked skopeo, recording every destination."""
    destinations: list[str] = []

    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"", b"")

    def record(*cmd: str, **_kwargs: object) -> AsyncMock:
        destinations.append(cmd[-1])
        return process

    with (
        patch("opi.connectors.skopeo.subprocess.run") as version_check,
        patch("opi.connectors.skopeo.decrypt_password_smart_auto_sync", return_value="token"),
        patch("asyncio.create_subprocess_exec", side_effect=record),
        patch("opi.connectors.skopeo.settings") as connector_settings,
        patch("opi.api.image_router.settings") as router_settings,
    ):
        version_check.return_value = MagicMock(returncode=0, stdout="skopeo version 1.14.0", stderr="")
        for mocked in (connector_settings, router_settings):
            mocked.REGISTRY_URL = REGISTRY_URL
            mocked.REGISTRY_ORG = REGISTRY_ORG
        connector_settings.REGISTRY_USERNAME = "rig+zad"
        connector_settings.REGISTRY_PASSWORD = "age:token"
        connector_settings.REGISTRY_VERIFY_TLS = True
        router_settings.IMAGE_UPLOAD_MAX_SIZE_MB = 10
        router_settings.TEMP_DIR = "/tmp"
        yield destinations


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(image_router)
    return TestClient(app)


def _push(client: TestClient, project: str, api_key: str, image_name: str = "backend", tag: str = "latest"):
    return client.post(
        f"/api/v1/projects/{project}/images/push?image_name={image_name}&tag={tag}",
        headers={"X-API-Key": api_key},
        files={"file": (f"{image_name}.tar", b"fake-tarball")},
    )


class TestPushOwnership:
    """The write half: project A cannot land on the tag project B pushed to."""

    def test_two_keys_same_image_and_tag_land_on_different_tags(self, client, two_projects, push_destinations):
        response_a = _push(client, PROJECT_A, KEY_A)
        response_b = _push(client, PROJECT_B, KEY_B)

        assert response_a.status_code == 200
        assert response_b.status_code == 200

        assert push_destinations == [
            f"docker://{PLATFORM_REPO}:{PROJECT_A}_backend-latest",
            f"docker://{PLATFORM_REPO}:{PROJECT_B}_backend-latest",
        ]
        assert response_a.json()["image"] == f"{PLATFORM_REPO}:{PROJECT_A}_backend-latest"
        assert response_b.json()["image"] == f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest"
        assert response_a.json()["image"] != response_b.json()["image"]

    def test_project_a_cannot_reach_the_tag_of_project_b(self, client, two_projects, push_destinations):
        """Whatever A supplies, the destination keeps A's owner prefix.

        This is the attack from the review: B runs 'backend:latest', A pushes the same
        names with its own key. A also tries to smuggle B's name in through image_name.
        """
        _push(client, PROJECT_B, KEY_B)
        b_destination = push_destinations[-1]

        for image_name in ("backend", f"{PROJECT_B}-backend", f"{PROJECT_B}_backend"):
            _push(client, PROJECT_A, KEY_A, image_name=image_name)
            assert push_destinations[-1] != b_destination
            assert push_destinations[-1].startswith(f"docker://{PLATFORM_REPO}:{PROJECT_A}_")

    def test_a_key_of_another_project_is_still_rejected(self, client, two_projects, push_destinations):
        response = _push(client, PROJECT_B, KEY_A)
        assert response.status_code == 401
        assert push_destinations == []

    def test_the_authenticated_project_owns_the_tag_not_the_path(self, client, push_destinations):
        """The owner comes from the key's project, not from the path segment.

        ``validate_api_token`` overwrites ``project_name`` with the store's project
        name, so a path that differs in case or shape cannot change the owner.
        """
        store = MagicMock()
        store.get.side_effect = {"PROJECT-A": _project(PROJECT_A, KEY_A)}.get
        with patch("opi.api.endpoint_util.get_project_store", return_value=store):
            response = _push(client, "PROJECT-A", KEY_A)

        assert response.status_code == 200
        assert push_destinations == [f"docker://{PLATFORM_REPO}:{PROJECT_A}_backend-latest"]


class TestReadOwnership:
    """The read half: a deployment may not point at another project's tag."""

    @pytest.fixture(autouse=True)
    def _registry_configured(self):
        with patch("opi.manager.project_validation.settings") as mocked:
            mocked.REGISTRY_URL = REGISTRY_URL
            mocked.REGISTRY_ORG = REGISTRY_ORG
            yield

    @staticmethod
    def _project_with_image(image: str) -> dict:
        return {
            "name": PROJECT_A,
            "deployments": [{"name": "production", "components": [{"reference": "web", "image": image}]}],
        }

    def test_own_image_is_accepted(self):
        data = self._project_with_image(f"{PLATFORM_REPO}:{PROJECT_A}_backend-latest")
        assert validate_platform_registry_image_ownership(data) == []

    def test_image_of_another_project_is_rejected(self):
        data = self._project_with_image(f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest")
        errors = validate_platform_registry_image_ownership(data)
        assert len(errors) == 1
        assert PROJECT_B in errors[0]
        assert "zelf gepusht" in errors[0]

    def test_legacy_unowned_tag_stays_usable(self):
        """Tags pushed before pinning have no owner and must keep working."""
        data = self._project_with_image(f"{PLATFORM_REPO}:backend-latest")
        assert validate_platform_registry_image_ownership(data) == []

    def test_an_uppercase_host_is_still_the_platform_registry(self):
        """A hostname is case-insensitive, so shouting it must not dodge the check."""
        image = f"{REGISTRY_URL.upper()}/{REGISTRY_ORG}:{PROJECT_B}_backend-latest"
        errors = validate_platform_registry_image_ownership(self._project_with_image(image))
        assert len(errors) == 1
        assert PROJECT_B in errors[0]

    def test_an_explicit_https_port_is_still_the_platform_registry(self):
        """':443' is the port the reference already implies, not another registry."""
        image = f"{REGISTRY_URL}:443/{REGISTRY_ORG}:{PROJECT_B}_backend-latest"
        errors = validate_platform_registry_image_ownership(self._project_with_image(image))
        assert len(errors) == 1
        assert PROJECT_B in errors[0]

    def test_a_digest_reference_into_the_platform_registry_is_refused(self):
        """A digest names an image in the shared repo without naming its owner."""
        digest = "sha256:" + "ab" * 32
        for image in (f"{PLATFORM_REPO}@{digest}", f"{PLATFORM_REPO}:{PROJECT_A}_backend-latest@{digest}"):
            errors = validate_platform_registry_image_ownership(self._project_with_image(image))
            assert len(errors) == 1, image
            assert "digest" in errors[0]

    def test_a_digest_outside_the_platform_registry_stays_free(self):
        digest = "sha256:" + "ab" * 32
        image = f"ghcr.io/rijksictgilde/algoritmeregister/backend@{digest}"
        assert validate_platform_registry_image_ownership(self._project_with_image(image)) == []

    def test_images_outside_the_platform_registry_are_not_judged(self):
        for image in (
            "ghcr.io/rijksictgilde/algoritmeregister/backend:project-b_thing-v1",
            "nginx:latest",
            f"{REGISTRY_URL}/other-org:{PROJECT_B}_backend-latest",
        ):
            assert validate_platform_registry_image_ownership(self._project_with_image(image)) == []

    def test_missing_registry_configuration_disables_the_check(self):
        with patch("opi.manager.project_validation.settings") as mocked:
            mocked.REGISTRY_URL = ""
            mocked.REGISTRY_ORG = ""
            data = self._project_with_image(f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest")
            assert validate_platform_registry_image_ownership(data) == []

    @pytest.mark.asyncio
    async def test_structure_validation_refuses_to_save_a_foreign_image(self):
        data = {
            "name": PROJECT_A,
            "components": [{"name": "web", "path": "/"}],
            "deployments": [
                {
                    "name": "production",
                    "components": [{"reference": "web", "image": f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest"}],
                }
            ],
        }
        with pytest.raises(ProjectIntegrityError, match=PROJECT_B):
            await validate_project_structure(data)
