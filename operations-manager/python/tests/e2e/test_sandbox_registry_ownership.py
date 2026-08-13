"""Sandbox E2E: twee projecten, twee echte sleutels, dezelfde image-naam (RC-98).

Bevinding A uit de technische review is een tenantscheidingsfout, en de enige
overtuigende toets is twee projecten die elkaar niet meer kunnen raken. Deze test
maakt daarom twee echte projecten aan, leest hun echte API-sleutels van hun
detailpagina's, en pusht met allebei dezelfde ``image_name`` en ``tag`` naar de echte
sandbox-registry. Daarna wordt in de registry zelf gekeken welke tags er staan.

Draaien:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    FORGEJO_URL=https://forgejo.sandbox.rijksapp.dev \
    FORGEJO_USER=rig-admin FORGEJO_PASSWORD=admin1234 \
    uv run pytest tests/e2e/test_sandbox_registry_ownership.py -m "e2e and sandbox" -v -o addopts=""
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_via_wizard
from tests.e2e.helpers.wizard import unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = FORGEJO_VERIFY_SSL

#: The sandbox registry, as configured in the operations-manager configmap.
REGISTRY_HOST = "registry.sandbox.rijksapp.dev"
REGISTRY_ORG = "rig"

#: Both projects push under exactly these names -- that is the point of the test.
IMAGE_NAME = "backend"
IMAGE_TAG = "latest"

#: A tiny image both pushes use, so the tarball stays small.
SOURCE_IMAGE = "busybox:latest"


@pytest.fixture(scope="module")
def image_tarball(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real docker-archive tarball, built with `docker save` like a customer would."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available to produce a docker-archive tarball")
    subprocess.run(["docker", "pull", SOURCE_IMAGE], check=True, capture_output=True, timeout=300)
    tarball = tmp_path_factory.mktemp("registry-ownership") / "image.tar"
    subprocess.run(["docker", "save", SOURCE_IMAGE, "-o", str(tarball)], check=True, capture_output=True, timeout=300)
    return tarball


def _create(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient) -> Generator[CreatedProject]:
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(),
            user_email=SANDBOX_TEST_USER["email"],
        )
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_API_VERIFY_SSL)


@pytest.fixture(scope="module")
def project_a(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient) -> Generator[CreatedProject]:
    yield from _create(sandbox_context, sandbox_url, forgejo)


@pytest.fixture(scope="module")
def project_b(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient) -> Generator[CreatedProject]:
    yield from _create(sandbox_context, sandbox_url, forgejo)


def _push(sandbox_url: str, project: CreatedProject, tarball: Path) -> httpx.Response:
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=300.0) as client, tarball.open("rb") as handle:
        return client.post(
            f"{sandbox_url}/api/v1/projects/{project.name}/images/push",
            params={"image_name": IMAGE_NAME, "tag": IMAGE_TAG},
            headers={"X-API-Key": project.api_key},
            files={"file": ("image.tar", handle, "application/x-tar")},
        )


def _registry_tags() -> list[str]:
    with httpx.Client(verify=True, timeout=60.0) as client:
        response = client.get(f"https://{REGISTRY_HOST}/v2/{REGISTRY_ORG}/tags/list", auth=("admin", "admin1234"))
    assert response.status_code == 200, response.text
    return response.json().get("tags") or []


def test_two_projects_pushing_the_same_name_do_not_collide(
    sandbox_url: str, project_a: CreatedProject, project_b: CreatedProject, image_tarball: Path
) -> None:
    """The heart of RC-98: two real keys, one image name, two destinations.

    Before the fix both pushes landed on ``backend-latest`` and the second overwrote the
    first, which Kubernetes then ran in the other tenant's namespace on the next rollout.
    """
    response_a = _push(sandbox_url, project_a, image_tarball)
    assert response_a.status_code == 200, response_a.text
    response_b = _push(sandbox_url, project_b, image_tarball)
    assert response_b.status_code == 200, response_b.text

    image_a = response_a.json()["image"]
    image_b = response_b.json()["image"]
    logger.info("project '%s' pushed to %s", project_a.name, image_a)
    logger.info("project '%s' pushed to %s", project_b.name, image_b)

    assert image_a == f"{REGISTRY_HOST}/{REGISTRY_ORG}:{project_a.name}_{IMAGE_NAME}-{IMAGE_TAG}"
    assert image_b == f"{REGISTRY_HOST}/{REGISTRY_ORG}:{project_b.name}_{IMAGE_NAME}-{IMAGE_TAG}"
    assert image_a != image_b

    # Not derived from the response: ask the registry which tags it now holds.
    tags = _registry_tags()
    assert f"{project_a.name}_{IMAGE_NAME}-{IMAGE_TAG}" in tags
    assert f"{project_b.name}_{IMAGE_NAME}-{IMAGE_TAG}" in tags
    assert f"{IMAGE_NAME}-{IMAGE_TAG}" not in tags, (
        "an unowned flat tag was written; the push is no longer supposed to be able to produce one"
    )


def test_a_project_cannot_push_with_another_projects_key(
    sandbox_url: str, project_a: CreatedProject, project_b: CreatedProject, image_tarball: Path
) -> None:
    """The key decides the owner, so borrowing another project's path changes nothing."""
    borrowed = CreatedProject(
        name=project_b.name,
        display_name=project_b.display_name,
        api_key=project_a.api_key,
        deployment_name=project_b.deployment_name,
    )
    response = _push(sandbox_url, borrowed, image_tarball)
    assert response.status_code == 401, response.text
