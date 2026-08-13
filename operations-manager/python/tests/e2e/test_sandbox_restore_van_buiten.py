"""
Sandbox E2E: de restore van BUITEN, met alleen een API-sleutel.

Dit is de positie waarin de zad-cli staat (vraag 10 en 11 uit
`plans/vragen-uit-zad-cli.md`): een API-sleutel, de twee leesendpoints, en geen
kennis van het projectbestand. De suite doet precies wat zij doen:

1. maak een project met een database- en een bucketdienst;
2. maak een backup via de API;
3. lees de naam uit `GET /api/v1/backup/runs/...` en uit
   `GET /api/v1/restore/snapshots/...`;
4. zet met DIE naam terug zonder doelvelden -- de weg uit RC-81;
5. zet terug naar een bestemming die niet resolvet -- de categorie uit RC-82.

Vereist een draaiende sandbox met JOUW build en E2E_BASE_URL. Draaien met:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=<SECRET_KEY van de sandbox> \
    uv run pytest tests/e2e/test_sandbox_restore_van_buiten.py -m "e2e and sandbox" \
      -o addopts="" -v -s
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_with_services
from tests.e2e.helpers.wizard import _unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_VERIFY_SSL = FORGEJO_VERIFY_SSL
_CLUSTER = "sandboxed-local"
_SERVICES = ["postgresql-database", "minio-storage"]


@pytest.fixture(scope="module")
def restore_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """Een project met een database en een bucket, plus een backup."""
    display_name = _unique_project_name(prefix="rest")
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            display_name,
            user_email=SANDBOX_TEST_USER["email"],
            services=_SERVICES,
        )
        logger.info("Project met database en bucket: %s (deployment %s)", created.name, created.deployment_name)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


def _api(sandbox_url: str, method: str, path: str, api_key: str, **kwargs: Any) -> httpx.Response:
    with httpx.Client(verify=_VERIFY_SSL, timeout=900.0) as client:
        return client.request(
            method,
            f"{sandbox_url.rstrip('/')}{path}",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            **kwargs,
        )


@pytest.mark.timeout(1800)
def test_restore_van_buiten(restore_project: CreatedProject, sandbox_url: str) -> None:
    project = restore_project.name
    deployment = restore_project.deployment_name
    key = restore_project.api_key
    namespace = f"rig-{project}"

    backup = _api(
        sandbox_url,
        "POST",
        f"/api/v1/backup/project/{project}/deployment/{deployment}",
        key,
        json={"resource_types": ["database", "minio"]},
    )
    logger.info("BACKUP %s %s", backup.status_code, backup.text[:2000])

    runs = _api(sandbox_url, "GET", f"/api/v1/backup/runs/{project}/{deployment}", key)
    logger.info("RUNS %s %s", runs.status_code, runs.text[:3000])

    snaps = _api(
        sandbox_url,
        "GET",
        f"/api/v1/restore/snapshots/{_CLUSTER}/{namespace}?project_name={project}",
        key,
    )
    logger.info("SNAPSHOTS %s %s", snaps.status_code, snaps.text[:3000])

    names = sorted(
        {
            item["reference_name"]
            for run in runs.json().get("runs", [])
            for item in run.get("items", [])
            if item.get("resource_type") == "database"
        }
    )
    logger.info("DATABASE-NAMEN UIT BACKUP LIST: %s", names)
    assert names, "backup list noemt geen enkele databasebackup"

    eigen = _api(
        sandbox_url,
        "POST",
        f"/api/v1/restore/database/{_CLUSTER}/{namespace}/{names[0]}?project_name={project}",
        key,
        json={},
    )
    logger.info("RESTORE EIGEN DATABASE %s %s", eigen.status_code, eigen.text[:2000])

    extern = _api(
        sandbox_url,
        "POST",
        f"/api/v1/restore/database/{_CLUSTER}/{namespace}/{names[0]}?project_name={project}",
        key,
        json={
            "target_database_host": "doel.invalid",
            "target_database_name": "d",
            "target_database_user": "u",
            "target_database_password": "g",
        },
    )
    logger.info("RESTORE DOEL.INVALID %s %s", extern.status_code, extern.text[:2000])

    assert eigen.status_code == 200, f"restore zonder doelvelden faalde: {eigen.status_code} {eigen.text[:500]}"
    assert extern.status_code == 400, f"doel.invalid gaf {extern.status_code}: {extern.text[:500]}"
    assert extern.json().get("error_category") == "InvalidTarget"
