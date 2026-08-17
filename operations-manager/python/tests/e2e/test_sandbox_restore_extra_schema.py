"""
Sandbox E2E: twee generatie-restores achter elkaar, met een extra schema erbij (RC-121).

Dit is de doorloop die in productie misging. Een project met een extra schema
(``{project}_{deployment}_rapportage``, RC-17) wordt TWEE keer teruggezet naar een
nieuwe generatie. De tweede keer was de breuk: de restore-pod koos zijn bronschema met
``pg_restore --list | grep " SCHEMA - " | head -1``, en die lijst is ALFABETISCH
gesorteerd. Bij generatie 2 staat ``..._rapportage`` (r) voor ``..._v2`` (v), dus werd
het rapportageschema hernoemd naar ``..._v3``: de applicatie las de rapportagetabellen,
haar eigen data bleef in ``..._v2`` staan en ``DATABASE_SCHEMA_RAPPORTAGE`` wees naar
een schema dat niet meer bestond. De restore meldde succes.

Deze suite meet dat op het cluster, met echte data in beide schema's:

1. maak een project met een databasedienst en voeg een extra schema toe;
2. zet in het standaardschema ``klanten`` en in het extra schema ``cijfers``, elk met
   een rij erin;
3. backup + restore (generatie 1) -- die ging altijd al goed;
4. backup + restore (generatie 2) -- de restore waar het op stukliep;
5. lees het geheim in de namespace en kijk in de database: staat ``klanten`` in het
   schema waar ``DATABASE_SCHEMA`` naar wijst, en bestaat het schema waar
   ``DATABASE_SCHEMA_RAPPORTAGE`` naar wijst nog, met ``cijfers`` erin?

Vereist een draaiende sandbox met JOUW build, E2E_BASE_URL en kubectl-toegang.
Draaien met:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=<SECRET_KEY van de sandbox> \
    uv run pytest tests/e2e/test_sandbox_restore_extra_schema.py -m "e2e and sandbox" \
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
_POSTFIX = "rapportage"


@pytest.fixture(scope="module")
def database_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    display_name = unique_project_name(prefix="schema")
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
    """Run SQL against the deployment's own database, from inside the cluster.

    With exactly the credentials the secret carries: that is what the application uses,
    so it is what the assertions have to be about.
    """
    result = subprocess.run(
        [
            "kubectl",
            "run",
            f"psql-rc121-{uuid.uuid4().hex[:8]}",
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


def _backup_and_restore(sandbox_url: str, project: str, deployment: str, key: str, *, round_name: str) -> None:
    """One full generation restore: back the database up, then restore that run."""
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


@pytest.mark.timeout(3600)
def test_twee_generatie_restores_met_een_extra_schema(database_project: CreatedProject, sandbox_url: str) -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze suite leest het geheim en de database")

    project = database_project.name
    deployment = database_project.deployment_name
    key = database_project.api_key
    namespace = f"rig-{project}"
    secret_name = f"{deployment}-database"

    # 1. Een extra schema erbij. De naam ervan draagt GEEN generatie, dus hij moet elke
    #    restore ongewijzigd doorkomen.
    task_id = sandbox_api.start_task(
        sandbox_url,
        "POST",
        f"/api/v2/projects/{project}/services/postgresql-database/schemas",
        key,
        {"postfix": _POSTFIX, "description": "RC-121"},
        verify_ssl=_VERIFY_SSL,
    )
    sandbox_api.wait_for_task(sandbox_url, task_id, key, verify_ssl=_VERIFY_SSL, timeout=900.0)

    schema_variable = f"DATABASE_SCHEMA_{_POSTFIX.upper()}"
    assert cluster.wait_for(
        lambda: schema_variable in _read_secret(namespace, secret_name),
        timeout=600.0,
        interval=10.0,
    ), f"{schema_variable} staat niet in het geheim; het extra schema is nooit doorgekomen"

    start = _read_secret(namespace, secret_name)
    logger.info(
        "VOOR DE RESTORES: db=%s schema=%s extra=%s",
        start["DATABASE_DB"],
        start["DATABASE_SCHEMA"],
        start[schema_variable],
    )

    # 2. Echte data in BEIDE schema's: zonder inhoud zegt een hernoeming niets.
    code, output = _psql(
        start,
        f'CREATE TABLE "{start["DATABASE_SCHEMA"]}".klanten (id int); '
        f'INSERT INTO "{start["DATABASE_SCHEMA"]}".klanten VALUES (1); '
        f'CREATE TABLE "{start[schema_variable]}".cijfers (id int); '
        f'INSERT INTO "{start[schema_variable]}".cijfers VALUES (2);',
    )
    assert code == 0, f"kon de testdata niet wegschrijven: {output[:1000]}"

    # 3. Twee generatie-restores achter elkaar. De EERSTE ging altijd al goed (de
    #    databasenaam zonder generatie is een prefix van de naam met postfix en sorteert
    #    dus vooraan); de TWEEDE is waar het op stukliep.
    for round_name in ("generatie 1", "generatie 2"):
        previous_db = start["DATABASE_DB"]
        _backup_and_restore(sandbox_url, project, deployment, key, round_name=round_name)
        # De refresh schrijft de nieuwe generatie naar zad-deployments; ArgoCD brengt hem
        # in het geheim. Daarop wachten is wachten op de TOESTAND, niet op de klok.
        assert cluster.wait_for(
            lambda db=previous_db: _read_secret(namespace, secret_name)["DATABASE_DB"] != db,
            timeout=600.0,
            interval=10.0,
        ), f"DATABASE_DB staat na '{round_name}' nog op {previous_db}; de nieuwe generatie is nooit doorgekomen"
        start = _read_secret(namespace, secret_name)
        logger.info("[%s] NA DE RESTORE: db=%s schema=%s", round_name, start["DATABASE_DB"], start["DATABASE_SCHEMA"])

    final = _read_secret(namespace, secret_name)
    logger.info(
        "EINDTOESTAND: db=%s schema=%s extra=%s",
        final["DATABASE_DB"],
        final["DATABASE_SCHEMA"],
        final.get(schema_variable),
    )

    # 4. De applicatie ziet haar EIGEN tabellen onder de naam die zij leest.
    code, output = _psql(final, f"SELECT to_regclass('\"{final['DATABASE_SCHEMA']}\".klanten')")
    assert code == 0, output[:1000]
    assert output not in ("", "\\N"), (
        f"tabel klanten staat niet in {final['DATABASE_SCHEMA']} (DATABASE_SCHEMA): {output[:500]}"
    )

    # 5. En het extra schema bestaat nog, met zijn eigen tabel, onder de naam waar
    #    DATABASE_SCHEMA_RAPPORTAGE naar wijst.
    assert schema_variable in final, f"{schema_variable} is uit het geheim verdwenen na de restores"
    code, output = _psql(final, f"SELECT to_regclass('\"{final[schema_variable]}\".cijfers')")
    assert code == 0, output[:1000]
    assert output not in ("", "\\N"), (
        f"tabel cijfers staat niet in {final[schema_variable]} ({schema_variable}): {output[:500]}"
    )
