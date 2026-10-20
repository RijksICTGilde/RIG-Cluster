"""Een database op de gedeelde PostgreSQL is alleen te openen door zijn eigen rollen (RC-285).

Dit is de helft die een unittest niet kan doen. Daar is een rol een string en een REVOKE een
verwachte aanroep; of PostgreSQL de verbinding daarna echt weigert, blijkt alleen tegen een
echte server. Twee projecten met een database op dezelfde gedeelde server, en de rol van het
ene probeert de database van het andere te openen.

De stand ``DATABASE_RESTRICT_PUBLIC_CONNECT`` moet hiervoor op ``on`` staan; dat doet de
sandbox-overlay van de configmap. Staat hij anders, dan slaat dit blok over in plaats van
rood te worden op een configuratiekeuze.

Wat hier NIET gemeten wordt, met de reden: een herstart van de pods van Keycloak, Forgejo en
de mailrelay. Dat is op een gedeeld cluster het werk van een ander platleggen. Wat die
herstart zou aantonen, namelijk dat de eigen rol nog binnenkomt, staat hieronder als het
recht zelf.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import pytest
from tests.e2e.conftest import SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import create_project_with_services
from tests.e2e.helpers.wizard import unique_project_name

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient
    from tests.e2e.helpers.lifecycle import CreatedProject

logger = logging.getLogger(__name__)

# Twee projecten aanmaken (600s elk) plus per toets een of twee psql-pods.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.sandbox,
    pytest.mark.slow,
    pytest.mark.timeout(2400),
]

_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

#: De gedeelde server is de standaard: ``scope`` staat op ``shared`` als een project niets zegt.
_SERVICES = ["publish-on-web", "postgresql-database"]

#: De platformdatabases, elk met de rol die ermee verbindt. Spiegelt
#: ``infrastructure/bootstrap/infrastructure/postgresql/database/base/databases.yaml``.
_PLATFORM = {"keycloak": "keycloak", "forgejo": "forgejo", "mailrelay": "mailrelay"}


def _maak_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
    prefix: str,
) -> Generator[CreatedProject]:
    page = sandbox_context.new_page()
    gemaakt: CreatedProject | None = None
    try:
        gemaakt = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(prefix=prefix),
            user_email=SANDBOX_TEST_USER["email"],
            services=_SERVICES,
            create_timeout=600.0,
        )
        logger.info("project %s, deployment %s", gemaakt.name, gemaakt.deployment_name)
        yield gemaakt
    finally:
        page.close()
        if gemaakt is not None:
            sandbox_api.delete_project_via_api(sandbox_url, gemaakt.name, gemaakt.api_key, verify_ssl=_VERIFY_SSL)


@pytest.fixture(scope="module")
def project_a(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient) -> Generator[CreatedProject]:
    yield from _maak_project(sandbox_context, sandbox_url, forgejo, "isoa")


@pytest.fixture(scope="module")
def project_b(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient) -> Generator[CreatedProject]:
    yield from _maak_project(sandbox_context, sandbox_url, forgejo, "isob")


@pytest.fixture(scope="module")
def secret_a(project_a: CreatedProject) -> dict[str, str]:
    return cluster.deployment_database_secret(project_a.name, project_a.deployment_name)


@pytest.fixture(scope="module")
def secret_b(project_b: CreatedProject) -> dict[str, str]:
    return cluster.deployment_database_secret(project_b.name, project_b.deployment_name)


@pytest.fixture(scope="module", autouse=True)
def alleen_met_de_stand_on() -> None:
    """De afsluiting is pas te meten als OPI in de stand ``on`` draait.

    Autouse en op moduleniveau, dus de overslag valt voor de projecten worden aangemaakt.
    """
    config = cluster.get_json_strict("get", "configmap", "operations-manager-config", "-n", "rig-system")
    if "DATABASE_RESTRICT_PUBLIC_CONNECT=on" not in (config.get("data") or {}).get(".env", ""):
        pytest.skip("OPI draait niet met DATABASE_RESTRICT_PUBLIC_CONNECT=on")


def _psql(
    secret: dict[str, str],
    sql: str,
    *,
    user: str = "",
    password: str = "",
    database: str = "",
    pogingen: int = 3,
) -> tuple[int, str]:
    """SQL tegen de gedeelde server, standaard met de read-write rol van de deployment."""
    return cluster.run_psql(
        sql,
        host=secret["DATABASE_SERVER_HOST"],
        user=user or secret["DATABASE_SERVER_USER"],
        database=database or secret["DATABASE_DB"],
        password=password or secret["DATABASE_PASSWORD"],
        pogingen=pogingen,
    )


def test_de_eigen_rol_opent_zijn_eigen_database(secret_a: dict[str, str]) -> None:
    """Zonder deze regel bewijst een geweigerde verbinding hieronder niets."""
    code, uit = _psql(secret_a, "SELECT current_database()")
    assert code == 0, f"de eigen database is niet te openen: {uit}"
    assert secret_a["DATABASE_DB"] in uit, uit


def test_de_rol_van_a_komt_niet_in_de_database_van_b(secret_a: dict[str, str], secret_b: dict[str, str]) -> None:
    """De kern: een rol opent alleen de databases waarvoor hij CONNECT heeft.

    Eén poging, want de uitkomst die we verwachten is juist "psql kwam niet binnen"; een
    herhaling zou daar alleen pods aan toevoegen.
    """
    code, uit = _psql(secret_a, "SELECT 1", database=secret_b["DATABASE_DB"], pogingen=1)
    assert code != 0, f"de rol van A opende de database van B: {uit}"
    assert "permission denied for database" in uit, f"andere fout dan een geweigerde database: {uit}"


def test_de_read_only_rol_opent_de_database_en_schrijft_niet(secret_a: dict[str, str]) -> None:
    ro, ro_wachtwoord = secret_a["DATABASE_SERVER_USER_RO"], secret_a["DATABASE_PASSWORD_RO"]
    code, uit = _psql(secret_a, "SELECT current_user", user=ro, password=ro_wachtwoord)
    assert code == 0, f"de read-only rol kan zijn eigen database niet openen: {uit}"
    assert ro in uit, uit

    schema = secret_a["DATABASE_SCHEMA"]
    _, uit = _psql(
        secret_a,
        f'CREATE TABLE "{schema}".rc285_mag_niet (x int)',
        user=ro,
        password=ro_wachtwoord,
        pogingen=1,
    )
    assert "permission denied" in uit, f"de read-only rol mocht schrijven: {uit}"


def test_public_heeft_geen_connect_op_de_databases_van_projecten(
    secret_a: dict[str, str], secret_b: dict[str, str]
) -> None:
    """Het recht zelf, naast het gedrag: PUBLIC staat niet meer in de ACL.

    Via een catalogusfunctie, die geen CONNECT op de database vraagt die hij beoordeelt.
    """
    namen = ", ".join(f"'{naam}'" for naam in (secret_a["DATABASE_DB"], secret_b["DATABASE_DB"]))
    code, uit = _psql(
        secret_a,
        f"SELECT datname, has_database_privilege('public', datname, 'CONNECT') "
        f"FROM pg_database WHERE datname IN ({namen}) ORDER BY datname",
    )
    assert code == 0, f"de ACL is niet te bevragen: {uit}"
    regels = [regel.strip() for regel in uit.splitlines() if "|" in regel]
    assert len(regels) == 2, f"niet beide databases gevonden: {uit}"
    assert all(regel.endswith("|f") for regel in regels), f"PUBLIC heeft nog CONNECT: {regels}"


def test_public_heeft_geen_connect_op_de_platformdatabases(secret_a: dict[str, str]) -> None:
    namen = ", ".join(f"'{naam}'" for naam in _PLATFORM)
    code, uit = _psql(
        secret_a,
        f"SELECT datname, has_database_privilege('public', datname, 'CONNECT') "
        f"FROM pg_database WHERE datname IN ({namen}) ORDER BY datname",
    )
    assert code == 0, f"de ACL is niet te bevragen: {uit}"
    regels = [regel.strip() for regel in uit.splitlines() if "|" in regel]
    assert len(regels) == len(_PLATFORM), f"niet elke platformdatabase gevonden: {uit}"
    assert all(regel.endswith("|f") for regel in regels), f"PUBLIC heeft nog CONNECT: {regels}"


def test_elke_platformrol_houdt_connect_op_zijn_eigen_database(secret_a: dict[str, str]) -> None:
    """Wat een herstart van die pods zou bewijzen, zonder een pod van een ander te raken.

    Het recht en niet een verbinding: een verbinding zou het wachtwoord van die rol nodig
    hebben, en dan meet een afwijkend wachtwoord in een secret mee met het recht dat hier
    het onderwerp is.
    """
    paren = ", ".join(f"('{database}', '{rol}')" for database, rol in sorted(_PLATFORM.items()))
    code, uit = _psql(
        secret_a,
        f"SELECT database, has_database_privilege(rol, database, 'CONNECT') "
        f"FROM (VALUES {paren}) AS t(database, rol) ORDER BY database",
    )
    assert code == 0, f"de rechten zijn niet te bevragen: {uit}"
    regels = [regel.strip() for regel in uit.splitlines() if "|" in regel]
    assert len(regels) == len(_PLATFORM), f"niet elke platformdatabase gevonden: {uit}"
    assert all(regel.endswith("|t") for regel in regels), f"een platformrol verloor CONNECT: {regels}"
