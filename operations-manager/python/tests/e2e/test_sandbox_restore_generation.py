"""
Sandbox E2E: twee restorerondes achter elkaar, en dan de rijen tellen (RC-123).

De meting die deze taak veroorzaakte. Een restore hoort de teruggezette data in een
database met een NIEUWE naam te zetten (``{db}``, ``{db}_v1``, ``{db}_v2``), maar elke
ronde meldde opnieuw ``0 -> 1``: de restore schreef de generatie deployment-breed weg en
las hem component-breed terug, dus het getal kwam nooit terug waar het opgehaald werd.
Bron en doel kregen daardoor dezelfde naam, ``pg_restore`` telt rijen op in plaats van ze
te vervangen, en een tweede restore verdubbelde de inhoud.

Deze suite meet dat op het cluster, met echte rijen:

1. maak een project met een databasedienst;
2. zet er een tabel met drie rijen in;
3. backup + restore (ronde 1);
4. backup + restore (ronde 2);
5. de twee rondes moeten TWEE VERSCHILLENDE databases opgeleverd hebben, en de
   eindtoestand moet nog steeds drie rijen tellen -- niet zes.

Het getal is de kern: een groene restore die "succes" meldt zei niets over waar de data
belandde. Vereist een draaiende sandbox met JOUW build, E2E_BASE_URL en kubectl-toegang.
Draaien met:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=<SECRET_KEY van de sandbox> \
    uv run pytest tests/e2e/test_sandbox_restore_generation.py -m "e2e and sandbox" \
      -o addopts="" -v -s
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import uuid
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_with_services
from tests.e2e.helpers.wizard import unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_VERIFY_SSL = FORGEJO_VERIFY_SSL
_SERVICES = ["postgresql-database"]
_ROWS = 3


@pytest.fixture(scope="module")
def database_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    display_name = unique_project_name(prefix="generatie")
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


def _psql(secret: dict[str, str], statement: str) -> tuple[int, str]:
    """Run SQL against the deployment's own database, with the credentials it uses."""
    result = subprocess.run(
        [
            "kubectl",
            "run",
            f"psql-rc123-{uuid.uuid4().hex[:8]}",
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
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            statement,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _backup_and_restore(sandbox_url: str, project: str, deployment: str, key: str, *, round_name: str) -> str:
    """One backup + restore round. Returns the database the restore says it wrote into."""
    backup = _api(
        sandbox_url,
        "POST",
        f"/api/v1/backup/project/{project}/deployment/{deployment}",
        key,
        json={"resource_types": ["database"]},
    )
    logger.info("[%s] BACKUP %s %s", round_name, backup.status_code, backup.text[:500])
    assert backup.status_code == 200, backup.text

    runs = _api(sandbox_url, "GET", f"/api/v1/backup/runs/{project}/{deployment}", key)
    assert runs.status_code == 200, runs.text
    run_ids = [run["backup_run_id"] for run in sorted(runs.json()["runs"], key=lambda r: r.get("timestamp", ""))]
    assert run_ids, f"geen backup-run gevonden na een geslaagde backup: {runs.text[:1000]}"

    restore = _api(
        sandbox_url,
        "POST",
        f"/api/v1/restore/project/{project}/deployment/{deployment}/run/{run_ids[-1]}",
        key,
        json={},
    )
    logger.info("[%s] RESTORE %s %s", round_name, restore.status_code, restore.text[:2000])
    assert restore.status_code == 200, restore.text
    body = restore.json()
    assert body["status"] == "success", body
    assert body["refresh_succeeded"] is True, body
    targets = [detail["target_pvc_name"] for detail in body["pvcs_restored"] if detail["success"]]
    assert len(targets) == 1, body
    return targets[0]


@pytest.mark.timeout(3600)
def test_twee_restores_geven_twee_databases_en_verdubbelen_de_rijen_niet(
    database_project: CreatedProject, sandbox_url: str
) -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze suite leest het geheim en de database")

    project = database_project.name
    deployment = database_project.deployment_name
    key = database_project.api_key
    namespace = f"rig-{project}"
    secret_name = f"{deployment}-database"

    start = _read_secret(namespace, secret_name)
    logger.info("VOOR DE RESTORES: db=%s schema=%s", start["DATABASE_DB"], start["DATABASE_SCHEMA"])

    # Echte rijen: het aantal is wat deze meting oplevert, "het werkt" zegt niets.
    values = ", ".join(f"({n})" for n in range(1, _ROWS + 1))
    code, output = _psql(
        start,
        f'CREATE TABLE "{start["DATABASE_SCHEMA"]}".klanten (id int); '
        f'INSERT INTO "{start["DATABASE_SCHEMA"]}".klanten VALUES {values};',
    )
    assert code == 0, f"kon de testdata niet wegschrijven: {output[:1000]}"

    databases: list[str] = []
    for round_name in ("ronde 1", "ronde 2"):
        target_db = _backup_and_restore(sandbox_url, project, deployment, key, round_name=round_name)
        databases.append(target_db)
        # De refresh schrijft de generatie naar zad-deployments; ArgoCD brengt hem in het
        # geheim. Wachten op de toestand die de restore ZELF noemt, niet op een klok.
        assert cluster.wait_for(
            lambda db=target_db: _read_secret(namespace, secret_name)["DATABASE_DB"] == db,
            timeout=600.0,
            interval=10.0,
        ), f"DATABASE_DB komt na '{round_name}' niet uit op {target_db}"
        logger.info("[%s] NA DE RESTORE: db=%s", round_name, target_db)

    # 1. De tweede ronde moet een ANDERE database opgeleverd hebben dan de eerste. Waren
    #    ze gelijk, dan schreef ronde 2 in de database die ronde 1 zojuist vulde.
    assert databases[0] != databases[1], (
        f"beide restorerondes kwamen uit op dezelfde database {databases[0]}: "
        f"de generatie loopt niet op en de tweede restore schrijft over de eerste heen"
    )
    assert databases[0].endswith("_v1"), databases
    assert databases[1].endswith("_v2"), databases

    # 2. En het aantal rijen is nog steeds wat het was. pg_restore telt rijen op, dus een
    #    restore in een gevulde database maakt er 2 x _ROWS van.
    final = _read_secret(namespace, secret_name)
    code, output = _psql(final, f'SELECT count(*) FROM "{final["DATABASE_SCHEMA"]}".klanten')
    assert code == 0, output[:1000]
    # _psql plakt stdout en stderr aan elkaar, en ``kubectl run --rm`` schrijft daar zijn
    # eigen 'pod ... deleted' achteraan. Het getal is de eerste regel.
    rows = output.splitlines()[0].strip()
    logger.info("EINDTOESTAND: db=%s schema=%s rijen=%s", final["DATABASE_DB"], final["DATABASE_SCHEMA"], rows)
    assert rows == str(_ROWS), (
        f"na twee restorerondes staan er {rows} rijen in {final['DATABASE_DB']}, verwacht {_ROWS}"
    )
