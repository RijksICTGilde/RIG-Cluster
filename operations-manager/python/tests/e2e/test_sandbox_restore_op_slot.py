"""
Sandbox E2E: na een versie-restore moet het project gewoon door kunnen.

De doorloop van RC-110 liep hier vast. Een restore van een backup-run maakt een
nieuwe generatie van de database en roteert daarbij het wachtwoord van de
databasegebruiker. Bleef het geheim in de namespace op het oude wachtwoord staan,
dan viel de refresh erna om op ``Manual intervention required to fix database user
or update secret`` -- en daarmee ook elke volgende wijziging op het project. De
API meldde intussen ``success``.

Deze suite doet precies de doorloop waar het op stukliep:

1. maak een project met een databasedienst;
2. maak een backup via de API;
3. zet die backup-run terug (de versie-weg, die het wachtwoord roteert);
4. lees het geheim in de namespace terug: nieuwe database, en het opgeslagen
   wachtwoord doet het echt (een verbinding, geen tekstvergelijking);
5. voeg daarna een component toe -- dat is de stap die eerder faalde.

Vereist een draaiende sandbox met JOUW build, E2E_BASE_URL en kubectl-toegang.
Draaien met:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=<SECRET_KEY van de sandbox> \
    uv run pytest tests/e2e/test_sandbox_restore_op_slot.py -m "e2e and sandbox" \
      -o addopts="" -v -s
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, CreatedProject, create_project_with_services
from tests.e2e.helpers.wizard import unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_VERIFY_SSL = FORGEJO_VERIFY_SSL
_SERVICES = ["postgresql-database"]


@pytest.fixture(scope="module")
def database_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    display_name = unique_project_name(prefix="slot")
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
        logger.info("Project met database: %s (deployment %s)", created.name, created.deployment_name)
        yield created
    finally:
        page.close()
        if created is not None and not os.environ.get("E2E_KEEP_PROJECT"):
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


def _api(sandbox_url: str, method: str, path: str, api_key: str, **kwargs: Any) -> httpx.Response:
    with httpx.Client(verify=_VERIFY_SSL, timeout=900.0) as client:
        return client.request(
            method,
            f"{sandbox_url.rstrip('/')}{path}",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            **kwargs,
        )


def _read_secret(namespace: str, secret_name: str) -> dict[str, str]:
    raw = subprocess.run(
        ["kubectl", "get", "secret", secret_name, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    data = json.loads(raw.stdout)["data"]
    return {key: base64.b64decode(value).decode("utf-8") for key, value in data.items()}


def _psql_accepts(secret: dict[str, str]) -> tuple[int, str]:
    """Connect to the database with exactly what the secret says, from inside the cluster.

    A text comparison against the previous password proves nothing: the whole failure
    was a secret that looked filled in and did not authenticate.
    """
    result = subprocess.run(
        [
            "kubectl",
            "run",
            f"psql-probe-{secret['DATABASE_DB'][-8:].replace('_', '-')}",
            "-n",
            "rig-system",
            "--rm",
            "-i",
            "--restart=Never",
            "--image=postgres:16-alpine",
            "--env",
            f"PGPASSWORD={secret['DATABASE_PASSWORD']}",
            "--command",
            "--",
            "psql",
            "-h",
            secret["DATABASE_SERVER_HOST"],
            "-U",
            secret["DATABASE_SERVER_USER"],
            "-d",
            secret["DATABASE_DB"],
            "-tAc",
            "SELECT 1",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


@pytest.mark.timeout(2400)
def test_wijziging_na_een_restore_slaagt(database_project: CreatedProject, sandbox_url: str) -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze suite leest het geheim in de namespace")

    project = database_project.name
    deployment = database_project.deployment_name
    key = database_project.api_key
    namespace = f"rig-{project}"
    secret_name = f"{deployment}-database"

    before = _read_secret(namespace, secret_name)
    logger.info("GEHEIM VOOR DE RESTORE: db=%s", before["DATABASE_DB"])

    backup = _api(
        sandbox_url,
        "POST",
        f"/api/v1/backup/project/{project}/deployment/{deployment}",
        key,
        json={"resource_types": ["database"]},
    )
    logger.info("BACKUP %s %s", backup.status_code, backup.text[:1000])
    assert backup.status_code == 200, backup.text

    # De backup-POST noemt de run zelf niet; de runlijst is de bron ervoor.
    runs = _api(sandbox_url, "GET", f"/api/v1/backup/runs/{project}/{deployment}", key)
    logger.info("RUNS %s %s", runs.status_code, runs.text[:2000])
    assert runs.status_code == 200, runs.text
    run_ids = sorted(run["backup_run_id"] for run in runs.json()["runs"])
    assert run_ids, f"geen backup-run gevonden na een geslaagde backup: {runs.text[:1000]}"
    run_id = run_ids[-1]

    restore = _api(
        sandbox_url,
        "POST",
        f"/api/v1/restore/project/{project}/deployment/{deployment}/run/{run_id}",
        key,
        json={},
    )
    logger.info("RESTORE %s %s", restore.status_code, restore.text[:2000])
    body = restore.json()

    # Eerst meten, dan pas oordelen: valt de refresh om, dan wil je in het verslag zien
    # WAT er in het geheim stond en of dat werkte.
    after = _read_secret(namespace, secret_name)
    logger.info(
        "GEHEIM NA DE RESTORE: db=%s wachtwoord-veranderd=%s",
        after["DATABASE_DB"],
        after["DATABASE_PASSWORD"] != before["DATABASE_PASSWORD"],
    )
    code, output = _psql_accepts(after)
    logger.info("PSQL MET HET GEHEIM: rc=%s %s", code, output[:1000])

    # 1. Het antwoord liegt niet: slaagde de refresh niet, dan staat dat er ook.
    assert restore.status_code == 200, f"restore niet volledig geslaagd: {restore.status_code} {restore.text[:1000]}"
    assert body["status"] == "success", body
    assert body["refresh_triggered"] is True, body
    assert body["refresh_succeeded"] is True, body

    # 2. De inloggegevens in het geheim doen het nog steeds. Het geheim zelf wordt door
    #    ArgoCD beheerd; de restore raakt het niet aan, en het wachtwoord blijft dus
    #    staan. Dat is precies wat de refresh erna nodig heeft om door te kunnen.
    assert after["DATABASE_PASSWORD"] == before["DATABASE_PASSWORD"], (
        "de restore heeft het wachtwoord toch geroteerd; de refresh kan dat geheim niet bijwerken"
    )
    assert code == 0, f"de opgeslagen inloggegevens werken niet: {output[:1000]}"

    # 3. De refresh heeft de nieuwe generatie naar zad-deployments geschreven, dus komt
    #    het geheim in de namespace daar via ArgoCD op uit.
    assert cluster.wait_for(
        lambda: _read_secret(namespace, secret_name)["DATABASE_DB"] != before["DATABASE_DB"],
        timeout=300.0,
        interval=10.0,
    ), f"DATABASE_DB in het geheim staat nog op {before['DATABASE_DB']}; de nieuwe generatie is nooit doorgekomen"

    # 4. De stap waar het op stukliep: een wijziging na de restore.
    task = sandbox_api.add_component(
        sandbox_url,
        project,
        key,
        component_name="na-restore",
        image=RUNNABLE_IMAGE,
        deployment_names=[deployment],
        verify_ssl=_VERIFY_SSL,
        timeout=600.0,
    )
    logger.info("COMPONENT NA DE RESTORE: %s", json.dumps(task)[:2000])
    assert task["status"] == "completed", task
