"""
Shared pytest fixtures for all tests.

This module provides common fixtures used across unit and integration tests.
"""

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
def mock_kubectl_connected() -> Any:
    """Mock KubectlConnector to appear connected without actual cluster."""
    with patch("opi.connectors.kubectl.KubectlConnector.isConnected", True):
        yield


@pytest.fixture
def mock_kubectl_logs() -> Any:
    """Mock kubectl log streaming for testing without a real cluster."""

    async def mock_get_logs(deployment_name: str, namespace: str, lines: int = 100) -> list[str]:
        return [f"2024-01-01T00:00:0{i}Z INFO: Log line {i} from {deployment_name}" for i in range(min(lines, 10))]

    # Patch where it's used (in logs_router), not where it's defined
    with patch("opi.api.logs_router.KubectlConnector") as mock_class:
        mock_instance = MagicMock()
        mock_instance.get_deployment_logs = mock_get_logs
        mock_class.return_value = mock_instance
        yield mock_class


@pytest.fixture
def mock_kubectl_command() -> Any:
    """Mock kubectl command execution."""

    async def mock_run(
        args: list[str],
        env: dict[str, str] | None = None,
        stdin_input: str | None = None,
    ) -> tuple[str, str, int]:
        return ("Success", "", 0)

    with patch("opi.connectors.kubectl.KubectlConnector._run_kubectl_command") as mock:
        mock.side_effect = mock_run
        yield mock


@pytest.fixture
def mock_session() -> dict[str, Any]:
    """Mock authenticated session for testing protected endpoints."""
    return {
        "user_id": "test-user-id",
        "email": "test@example.com",
        "projects": ["test-project"],
        "created_at": "2024-01-01T00:00:00Z",
        "roles": ["admin"],
    }


@pytest.fixture
def mock_project_service() -> Any:
    """Mock project service for testing without database."""

    class MockProjectInfo:
        def __init__(self, name: str, api_key: str = "test-api-key-12345") -> None:
            self.name = name
            self.api_key = api_key
            self.data = {
                "deployments": [
                    {
                        "name": "main",
                        "cluster": "local",
                        "components": [
                            {"reference": "web"},
                            {"reference": "api"},
                        ],
                    }
                ]
            }

    class MockProjectStore:
        """Mirrors the ProjectStore read interface.

        Deliberately does NOT expose the old ProjectService methods
        (get_project/get_all_projects). This fixture patches get_project_store,
        so offering both interfaces would let production drift onto one of them
        while the tests keep passing against the other -- which is exactly how
        the logs endpoints ended up broken behind a green suite.
        """

        def __init__(self) -> None:
            self._projects = {
                "test-project": MockProjectInfo("test-project", "test-api-key-12345"),
            }

        def get(self, project_name: str) -> MockProjectInfo | None:
            return self._projects.get(project_name)

        def get_all(self) -> list[MockProjectInfo]:
            return list(self._projects.values())

    # Patch in both locations: endpoint_util (for auth) and logs_router (for usage)
    mock_service = MockProjectStore()
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.logs_router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def mock_settings() -> Any:
    """Mock settings for testing."""
    with patch("opi.core.config.settings") as mock_settings:
        mock_settings.CLUSTER_MANAGER = "local"
        mock_settings.DEBUG = True
        mock_settings.SECRET_KEY = "test-secret-key-for-testing-only"
        mock_settings.OIDC_DISABLED = True
        mock_settings.ENABLE_GIT_MONITOR = False
        mock_settings.KEYCLOAK_URL = ""
        mock_settings.PROMETHEUS_EXTERNAL_URL = ""
        mock_settings.PROMETHEUS_URL = ""
        # Real int, not a bare MagicMock: create_app() reads this to wire SessionMiddleware,
        # and numeric comparisons on it must work even when this mock is in effect.
        mock_settings.SESSION_MAX_AGE_SECONDS = 28800
        yield mock_settings


@pytest.fixture
def api_key() -> str:
    """API key for testing authenticated endpoints."""
    return "test-api-key-12345"


@pytest.fixture
def test_client(mock_settings: Any) -> TestClient:
    """
    Synchronous test client for simple API tests.

    Note: For WebSocket tests, use async_client instead.
    """
    # Import here to avoid circular imports and ensure mocks are applied
    from opi.server import create_app

    app = create_app()
    return TestClient(app)


@pytest.fixture
async def async_client(mock_settings: Any) -> AsyncGenerator[AsyncClient]:
    """
    Async test client for WebSocket and async endpoint tests.

    Example usage:
        async def test_endpoint(async_client):
            response = await async_client.get("/api/health")
            assert response.status_code == 200
    """
    from opi.server import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client


@pytest.fixture
def temp_kubeconfig(tmp_path: Any) -> str:
    """Create a temporary kubeconfig file for testing."""
    kubeconfig_content = """
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://test-cluster:6443
    certificate-authority-data: dGVzdC1jYQ==
  name: test-cluster
contexts:
- context:
    cluster: test-cluster
    user: test-user
  name: test-context
current-context: test-context
users:
- name: test-user
  user:
    token: test-token
"""
    kubeconfig_path = tmp_path / "kubeconfig"
    kubeconfig_path.write_text(kubeconfig_content)
    return str(kubeconfig_path)


@pytest.fixture
def mock_cluster_config() -> Any:
    """Mock cluster configuration for testing."""
    # Patch where it's used (in logs_router), not where it's defined
    with patch("opi.api.logs_router.get_prefixed_namespace") as mock:
        mock.return_value = "test-namespace"
        yield mock


@pytest.fixture(autouse=True)
def reset_kubectl_singleton() -> Any:
    """Reset KubectlConnector singleton between tests."""
    from opi.connectors.kubectl import KubectlConnector

    # Reset singleton state
    KubectlConnector._instance = None
    yield
    # Clean up after test
    KubectlConnector._instance = None


@pytest.fixture(autouse=True)
def reset_readiness_state() -> Any:
    """Reset readiness singleton and mark all services as ready for tests."""
    import opi.core.readiness as readiness_module

    # Reset the singleton so each test starts fresh
    readiness_module._state = None
    state = readiness_module.get_readiness_state()
    state.database.mark_ready()
    state.keycloak.mark_ready()
    state.oauth.mark_ready()
    state.projects.mark_ready()
    yield
    # Clean up after test
    readiness_module._state = None


# --- Real Postgres for ORM-backed repository tests (RC-5 persistence phase 2) --------
# A throwaway Postgres (testcontainers) so service-owned ORM repositories are tested
# against real SQL -- ON CONFLICT uniqueness, transactions -- not mocks. Session-scoped
# container; each `orm_db` test starts from a truncated schema.


#: Ons eigen etiket op de wegwerp-Postgres. Testcontainers zet er zelf ook een op
#: (``org.testcontainers``), maar dat draagt elk project dat deze bibliotheek gebruikt, en
#: op deze machine draaien er meer. Wij ruimen alleen op wat van ons is.
ORM_CONTAINER_LABEL = "nl.rijksapp.zad.orm-test"


def _ruim_achtergebleven_containers_op() -> None:
    """Weg met wat een vorige run heeft laten staan.

    Wie ze maakt, ruimt ze op, en dat moet ook gelden als de vorige run NIET netjes
    eindigde. De context manager hieronder stopt de container bij een normale afloop, maar
    bij een harde onderbreking (ctrl-c, een gekilde sessie, een timeout) loopt hij niet af
    en blijft er een Postgres draaien. Er stonden er zo vier tegelijk, waarvan de oudste
    drie dagen.

    Normaal is dat het werk van Ryuk, de opruimsidecar van testcontainers. Die kan hier
    niet: hij mount de dockersocket, en op Docker Desktop staat die onder
    ``~/.docker/run/docker.sock``, wat de daemon weigert te mounten ("operation not
    supported"). Met ``TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`` start hij wel, maar dan is
    zijn poort niet te bereiken. Dus doen we het zelf, en dan ook echt zelf: bij het
    STARTEN van een run, want dat is het enige moment waarop we zeker weten dat we draaien.

    Faalt Docker of ontbreekt hij, dan gebeurt er niets. Opruimen mag nooit de reden zijn
    dat een suite niet start.
    """
    import subprocess

    try:
        gevonden = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={ORM_CONTAINER_LABEL}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        containers = gevonden.stdout.split()
        if containers:
            subprocess.run(["docker", "rm", "-f", *containers], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return


@pytest.fixture(scope="session")
def _orm_pg_container():
    # Ryuk is testcontainers' reaper sidecar; op Docker Desktop komt hij niet overeind (zie
    # _ruim_achtergebleven_containers_op voor het waarom, gemeten en niet aangenomen).
    # ``setdefault`` zodat CI hem terug kan zetten, want daar werkt hij wel.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

    from testcontainers.postgres import PostgresContainer

    _ruim_achtergebleven_containers_op()

    # Het etiket is wat het opruimen mogelijk maakt: zonder dat weten we bij de volgende
    # run niet welke van deze containers van ons was.
    container = PostgresContainer("postgres:16-alpine").with_kwargs(labels={ORM_CONTAINER_LABEL: "true"})
    with container:
        yield container


@pytest.fixture
async def orm_db(_orm_pg_container):
    from opi.core.db import Base, configure_engine, create_all_orm_tables, dispose_engine, session_scope
    from sqlalchemy import text

    url = _orm_pg_container.get_connection_url().replace("+psycopg2", "+asyncpg")
    configure_engine(url)
    await create_all_orm_tables()
    tables = ", ".join(Base.metadata.tables)
    async with session_scope() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield
    await dispose_engine()


# --- Live voortgang van een lange run ------------------------------------------------
#
# Een sandboxrun duurt bijna een uur en pytest zegt tot het EIND niets bruikbaars: met -q
# krijg je punten, met -v een regel zonder tijd, en de samenvatting pas na afloop. Wie de
# run niet zelf voor zich heeft (een dispatchte sessie, een collega die meekijkt) ziet dus
# niets en kan niet beoordelen of het loopt, hoe snel, of waar het strandde. Dat kostte in
# RC-108 meerdere keren de verkeerde conclusie: een suite die gewoon vorderde werd voor
# vastgelopen aangezien, en een afgebroken run liet geen enkele oorzaak achter.
#
# Dit schrijft per afgeronde test EEN regel weg, met de gegevens die pytest zelf levert:
# ``report.outcome``, ``report.duration`` en ``report.nodeid``. Geen tekst uit de uitvoer
# raden - dat is precies de onbetrouwbaarheid die deze doorloop op meer plekken opleverde.
#
#     PYTEST_VOORTGANG=/tmp/voortgang.txt uv run pytest ...
#     tail -f /tmp/voortgang.txt
#
# Zonder die variabele doet dit niets, dus een gewone run verandert er niet van.

_VOORTGANG_PAD = os.environ.get("PYTEST_VOORTGANG")
_voortgang_stand = {"klaar": 0, "totaal": 0, "rood": 0}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list) -> None:
    """Onthoud hoeveel tests er gaan draaien, zodat elke regel 'n van totaal' kan tonen.

    ``trylast``, want de deselectie op markers (``-m e2e``) gebeurt ook in deze hook: tel je
    eerder, dan staat er 9054 als totaal terwijl er 462 tests draaien.
    """
    _voortgang_stand["totaal"] = len(items)


def pytest_runtest_logreport(report: Any) -> None:
    """Schrijf een regel zodra een test klaar is.

    Alleen op de call-fase, behalve als setup of teardown faalt (dan is DAT de uitkomst en
    zou een test anders stil ontbreken in de lijst - wat bij een module-scoped fixture de
    hele groep onzichtbaar maakt) en behalve een skip in setup, want dat is de gewone vorm
    van overslaan.

    Alleen een echte failure telt als rood: een skip en een verwachte failure zijn een
    groene run, en een meetinstrument dat die rood meldt is precies de faalmodus die deze
    tak opruimt.
    """
    if not _VOORTGANG_PAD:
        return
    if report.when != "call" and not (report.failed or report.skipped):
        return
    _voortgang_stand["klaar"] += 1
    if report.skipped:
        uitslag = "XFAIL" if hasattr(report, "wasxfail") else "SKIP"
    elif report.passed:
        uitslag = "XPASS" if hasattr(report, "wasxfail") else "PASSED"
    else:
        uitslag = "FAILED" if report.when == "call" else "ERROR"
        _voortgang_stand["rood"] += 1
    regel = (
        f"{datetime.now(UTC).strftime('%H:%M:%S')}  "
        f"{_voortgang_stand['klaar']:3d}/{_voortgang_stand['totaal']:<3d}  "
        f"{report.duration:6.1f}s  "
        f"rood={_voortgang_stand['rood']:<2d} "
        f"{uitslag:<6} {report.nodeid}\n"
    )
    with open(_VOORTGANG_PAD, "a", encoding="utf-8") as bestand:
        bestand.write(regel)
