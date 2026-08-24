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
    from collections.abc import AsyncGenerator, Iterator


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
        # Om dezelfde reden een echte string. validate_master_api_key doet eerst
        # `if not settings.MASTER_API_KEY` (een kale MagicMock is waar-achtig, dus daar komt
        # hij doorheen) en daarna secrets.compare_digest, en die weigert een MagicMock met
        # een TypeError. Het endpoint gaf dan 500 in plaats van 401, en de test die juist
        # toetst dat een PROJECTsleutel geweigerd wordt viel om op de vorm van de weigering.
        # De waarde is expres een andere dan de projectsleutels in de fixtures.
        mock_settings.MASTER_API_KEY = "test-master-api-key-not-a-project-key"
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
# Een ECHTE Postgres, zodat de ORM-repositories van de diensten tegen echt SQL draaien
# (ON CONFLICT-uniciteit, transacties) en niet tegen mocks.
#
# Eén server met een vaste naam, die blijft staan; per pytest-sessie een eigen database
# erin. Dat is de hele regeling, en ze vervangt drie mechanismen die er eerder omheen
# stonden (een wegwerpcontainer per run, een label met de pid van de maker, een veeg over
# achtergebleven containers, en Ryuk met een socket-override). Wat die moesten dekken was
# steeds hetzelfde: een run die hard eindigt laat zijn Postgres draaien, want die container
# hangt onder de Docker-daemon en niet onder pytest. Gemeten op 20 augustus 2026 stonden er
# elf, de oudste 45 uur.
#
# Een container die per definitie blijft staan kan dat niet lekken. Er is er één, hij heet
# altijd hetzelfde, en opruimen is gewoon werk dat een mens doet:
#
#     task test-db-stop     # docker rm -f zad-test-postgres
#     task test-db-reset    # weg en opnieuw
#
# Het lek verhuist daarmee naar de databases IN die server, en dat is een ruil die de
# moeite waard is: die kosten niets, zijn onzichtbaar, en de veeg hieronder haalt ze weg
# zodra hun maker dood is.

#: Vaste naam, want daar draait dit hele ontwerp om: wat een mens kan noemen, kan hij
#: opruimen.
ZAD_TEST_PG_CONTAINER = "zad-test-postgres"
ZAD_TEST_PG_IMAGE = "postgres:16-alpine"
ZAD_TEST_PG_PASSWORD = "zadtest"
#: Alleen van belang bij het AANMAKEN. Staat de container er al, dan lezen we zijn
#: werkelijke poort uit Docker: die is leidend, anders praat een tweede run tegen een
#: poort waar niets luistert.
ZAD_TEST_PG_PORT = os.environ.get("ZAD_TEST_PG_PORT", "55432")
#: Prefix van de database per run. ``zad_test_<pid>``: de pid maakt een verweesde database
#: herkenbaar, precies zoals het pid-label dat eerder voor containers deed.
ZAD_TEST_DB_PREFIX = "zad_test_"


class TestPostgresError(RuntimeError):
    """De test-Postgres is er niet en kan er niet komen. Luid falen, nooit stil."""


def _docker(*args: str, check: bool = True, timeout: int = 60):
    import subprocess

    try:
        klaar = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise TestPostgresError(
            "docker niet gevonden; de ORM-tests hebben een echte Postgres nodig. Start Docker en draai opnieuw."
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise TestPostgresError(f"docker {' '.join(args)} mislukte: {exc}") from exc
    if check and klaar.returncode != 0:
        raise TestPostgresError(f"docker {' '.join(args)} gaf {klaar.returncode}: {klaar.stderr.strip()}")
    return klaar


def _container_staat() -> tuple[bool, str]:
    """(draait hij, op welk image). Bestaat hij niet, dan (False, "")."""
    klaar = _docker("inspect", "--format", "{{.State.Running}} {{.Config.Image}}", ZAD_TEST_PG_CONTAINER, check=False)
    if klaar.returncode != 0:
        return False, ""
    draait, _, image = klaar.stdout.strip().partition(" ")
    return draait == "true", image


def _wacht_tot_hij_luistert(seconden: int = 60) -> None:
    import time

    einde = time.monotonic() + seconden
    while time.monotonic() < einde:
        if _docker("exec", ZAD_TEST_PG_CONTAINER, "pg_isready", "-U", "postgres", check=False).returncode == 0:
            return
        time.sleep(0.5)
    logboek = _docker("logs", "--tail", "20", ZAD_TEST_PG_CONTAINER, check=False).stdout
    raise TestPostgresError(f"'{ZAD_TEST_PG_CONTAINER}' werd niet klaar binnen {seconden}s. Laatste regels:\n{logboek}")


def _zorg_voor_container() -> str:
    """Start de vaste Postgres als hij er niet is, en geef zijn poort op de host terug.

    Draait hij al, dan blijft hij draaien: hergebruik is het punt. Draait hij op een ander
    image dan we hier vragen, dan gaat hij eraf en komt hij terug -- anders test een
    volgende versie stilzwijgend tegen de oude.
    """
    draait, image = _container_staat()
    if draait and image != ZAD_TEST_PG_IMAGE:
        _docker("rm", "-f", ZAD_TEST_PG_CONTAINER)
        draait = False
    elif not draait and image:
        # Bestaat maar staat stil (machine herstart, handmatig gestopt): opnieuw beginnen
        # is voorspelbaarder dan een oude container weer aanzetten.
        _docker("rm", "-f", ZAD_TEST_PG_CONTAINER)

    if not draait:
        gemaakt = _docker(
            "run",
            "-d",
            "--name",
            ZAD_TEST_PG_CONTAINER,
            "-e",
            f"POSTGRES_PASSWORD={ZAD_TEST_PG_PASSWORD}",
            "-p",
            f"{ZAD_TEST_PG_PORT}:5432",
            ZAD_TEST_PG_IMAGE,
            check=False,
            timeout=180,
        )
        # Twee suites die tegelijk beginnen zien allebei geen container en doen allebei dit
        # commando; een van de twee krijgt "name already in use". Dat is geen fout maar het
        # antwoord: de ander was eerder. Alleen als er daarna nog steeds niets draait, is er
        # echt iets mis.
        if gemaakt.returncode != 0 and not _container_staat()[0]:
            raise TestPostgresError(
                f"'{ZAD_TEST_PG_CONTAINER}' kon niet starten: {gemaakt.stderr.strip()}\n"
                f"Zit poort {ZAD_TEST_PG_PORT} bezet, zet dan ZAD_TEST_PG_PORT."
            )

    _wacht_tot_hij_luistert()
    poort = _docker("port", ZAD_TEST_PG_CONTAINER, "5432/tcp").stdout.strip().splitlines()[0]
    return poort.rsplit(":", 1)[1]


def _psql(sql: str) -> str:
    """Eén statement op de onderhoudsdatabase, via de container zelf.

    Bewust met ``docker exec`` en niet met een driver: dit draait in een sessie-fixture,
    die synchroon is, en zo hoeft er geen tweede verbindingsweg (en geen event loop) naast
    die van de tests te bestaan.
    """
    klaar = _docker(
        "exec",
        "-e",
        f"PGPASSWORD={ZAD_TEST_PG_PASSWORD}",
        ZAD_TEST_PG_CONTAINER,
        "psql",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-tAc",
        sql,
    )
    return klaar.stdout.strip()


def _maker_leeft(pid_tekst: str) -> bool:
    """Leeft het proces dat dit maakte nog?

    Geen leesbare pid betekent iets van voor deze regeling, en die run is hoe dan ook
    voorbij: niet levend dus. Een pid die we niet mogen signaleren (PermissionError) leeft
    wel; alleen kijken mag altijd, dus die fout betekent "bestaat, maar is niet van ons".
    """
    try:
        pid = int(pid_tekst)
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ruim_verweesde_databases_op() -> None:
    """Weg met de databases van runs die niet meer draaien.

    Hergebruik van pids kan een wees even laten staan; die valt bij een volgende run
    alsnog om. Een database van een LEVENDE run blijft staan, en dat is niet vrijblijvend:
    hier draaien suites naast elkaar (agents in eigen worktrees), en dat is precies waarom
    elke run zijn eigen database heeft in plaats van zijn eigen container.
    """
    namen = _psql(f"SELECT datname FROM pg_database WHERE datname LIKE '{ZAD_TEST_DB_PREFIX}%'")
    for naam in namen.splitlines():
        naam = naam.strip()
        if not naam or _maker_leeft(naam.removeprefix(ZAD_TEST_DB_PREFIX)):
            continue
        _psql(f'DROP DATABASE IF EXISTS "{naam}" WITH (FORCE)')


@pytest.fixture(scope="session")
def _orm_db_url() -> Iterator[str]:
    """De verbinding naar de database van DEZE run, in de gedeelde server."""
    poort = _zorg_voor_container()
    _ruim_verweesde_databases_op()

    naam = f"{ZAD_TEST_DB_PREFIX}{os.getpid()}"
    # IF EXISTS, want een pid kan hergebruikt zijn en de vorige eigenaar is dan dood.
    _psql(f'DROP DATABASE IF EXISTS "{naam}" WITH (FORCE)')
    _psql(f'CREATE DATABASE "{naam}"')
    try:
        yield f"postgresql+asyncpg://postgres:{ZAD_TEST_PG_PASSWORD}@127.0.0.1:{poort}/{naam}"
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{naam}" WITH (FORCE)')


@pytest.fixture
async def orm_db(_orm_db_url):
    from opi.core.db import Base, configure_engine, create_all_orm_tables, dispose_engine, session_scope
    from sqlalchemy import text

    configure_engine(_orm_db_url)
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
