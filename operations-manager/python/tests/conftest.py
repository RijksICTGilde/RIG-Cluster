"""
Shared pytest fixtures for all tests.

This module provides common fixtures used across unit and integration tests.
"""

import os

# Voor elke import van opi: setup_components leest LOTC_STRICT tijdens het importeren van
# opi.core.templates_lotc, dus na die import zetten doet niets meer. Strict weigert een
# onbekende attribuutWAARDE in plaats van hem stil te renderen, en staat daarom in de
# tests en in CI en niet in de productie-image.
os.environ.setdefault("LOTC_STRICT", "1")

# ENVIRONMENT valt zonder instelling terug op "production", en dan registreert create_app
# de proef- en testroutes niet. De tests meten die routes, dus hier expliciet "local".
os.environ.setdefault("ENVIRONMENT", "local")

import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from opi.core.db import Base

# Load every app module with the real settings before any test patches them. A module first
# imported while mock_settings is active binds `settings` to the MagicMock for the rest of the
# session, so a later test fails or passes depending on which tests ran before it.
from opi.server import create_app
from opi.services.user_otp_service import OtpStatus, OtpVerification
from opi.utils.sops import generate_sops_key_pair
from sqlalchemy import Engine, create_engine, text
from tests.docker_runtime import DockerError, container_state, docker

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterator


def write_private_file(path: Path, content: str) -> Path:
    """Write a key or token file the way ``age-keygen -o`` leaves one: owner-readable only.

    ``require_private_file`` refuses anything wider, so a test that writes one with the
    default umask is testing a file no tool here accepts.
    """
    path.write_text(f"{content}\n")
    path.chmod(0o600)
    return path


@pytest.fixture(scope="session")
def make_age_keypair() -> Callable[[], tuple[str, str]]:
    """A factory that mints a throwaway AGE keypair, generated for this test run only.

    A test that needs a key makes one, so no fixed key has to sit in the tree. That is what keeps
    the scanner believable: it alarms on any valid ``AGE-SECRET-KEY-``, and an alarm with known
    findings in it is one people learn to walk around. A working private key in the tree also
    opens the encrypted values that sit elsewhere in it, and pytest prints the decrypted value
    on a red assertion.

    Session scope, because ``age-keygen`` is a subprocess and the pair is immutable -- but the
    factory is called per test that wants one, so a test needing two distinct keys just calls it
    twice.

    Requires the ``age-keygen`` binary. Tests that use this should skip without it, the way
    ``test_sops_skip_unchanged`` and the rotation tests do.
    """

    def make() -> tuple[str, str]:
        """Returns (private_key, public_key)."""
        return generate_sops_key_pair()

    return make


@pytest.fixture(scope="session")
def age_keypair(make_age_keypair: Callable[[], tuple[str, str]]) -> tuple[str, str]:
    """One throwaway AGE keypair as ``(private_key, public_key)``, for the whole run."""
    return make_age_keypair()


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
            # Leden en hun tokens (RC-322): het handhavingspunt zoekt hier eerst, en valt
            # pas daarna terug op de oude projectsleutel hierboven.
            self.users: list[Any] = []
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
        # De body-limit (RC-367) rekent met deze twee bij elk verzoek op een uploadroute.
        mock_settings.ATTACHMENT_BODY_LIMIT_FACTOR = 16
        mock_settings.IMAGE_UPLOAD_MAX_SIZE_MB = 5120
        # Een echte lege string: /health zet het proefdoel-token als responseheader zodra
        # het waar-achtig is, en een kale MagicMock is dat. Starlette weigerde die header
        # met een TypeError, waardoor /health onder deze fixture niet te meten was.
        mock_settings.SECURITY_PROOF_TOKEN = ""
        # Ook echte ints, om dezelfde reden: opi/api/rate_limiter.py rekent bij import met
        # deze waarden om zijn emmers te bouwen, en retry_after_seconds vergelijkt met de
        # max. Een kale MagicMock gaf een TypeError bij de import van de module, en dan
        # viel elke test die create_app() bouwt om op de fixture.
        mock_settings.AUTH_FAILURE_LIMIT_PER_MINUTE = 10
        mock_settings.API_RATE_LIMIT_PER_MINUTE = 600
        mock_settings.API_RATE_LIMIT_BURST = 120
        mock_settings.RATE_LIMIT_RETRY_AFTER_MAX_SECONDS = 60
        # Zelfde reden: opi/utils/client_ip.py vergelijkt TRUSTED_PROXY_HOPS met een int
        # (`hops > 0`), en een kale MagicMock geeft daar dezelfde TypeError.
        mock_settings.TRUSTED_PROXY_HOPS = 0
        yield mock_settings


@pytest.fixture
def clone_route_without_guard_and_encryption() -> Iterator[None]:
    """Een kloonroute toetsen op haar payload, niet op de bron-guard en de versleuteling.

    De guard doet een naamsopzoeking en de versleuteling vraagt de AGE-sleutel van het
    cluster. Beide hebben hun eigen tests in ``test_external_clone_source.py``.
    """

    async def _unencrypted(secret: str) -> str:
        return secret

    with (
        patch("opi.api.router.clone_source_refusal", AsyncMock(return_value=None)),
        patch("opi.api.v2.router.clone_source_refusal", AsyncMock(return_value=None)),
        patch("opi.api.router._encrypt_for_task", _unencrypted),
    ):
        yield


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


@pytest.fixture
def stub_platform_repo_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a refresh handler's git sync out of a test that does not measure it.

    ``handle_refresh_project`` and ``handle_refresh_deployment`` pull in external commits
    in the deployments repo and the argo repo (RC-423), and that is real network.
    """

    async def nothing() -> None:
        return None

    monkeypatch.setattr("opi.core.task_handlers_operations.sync_platform_repos", nothing)


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


@pytest.fixture(autouse=True)
def elevated_by_default() -> Any:
    """A test of what a platform admin may do starts elevated; a test of the opposite says so.

    ``tests/test_elevated_platform_admin.py`` covers the admin without a step-up. The state
    is only set here for calls that skip the middleware, which sets it on every request.
    """
    from opi.core.step_up import step_up_fresh

    token = step_up_fresh.set(True)
    yield
    step_up_fresh.reset(token)


@pytest.fixture
def api_step_up() -> Any:
    """The bearer token of this test carries a fresh step-up, for a token with a ``sid``."""
    with patch("opi.services.api_step_up_service.ApiStepUpService.is_fresh", AsyncMock(return_value=True)):
        yield


@pytest.fixture(autouse=True)
def no_api_step_up() -> Any:
    """No API caller has a step-up unless a test gives it one, and no test needs the table for that."""
    with patch("opi.services.api_step_up_service.ApiStepUpService.is_fresh", AsyncMock(return_value=False)):
        yield


@pytest.fixture(autouse=True)
def reset_actor_context() -> Any:
    """Reset the actor contextvar between tests, so one test's set_actor call can never
    leak into another test's assertions."""
    from opi.core.actor import ACTOR_UNKNOWN, actor

    token = actor.set(ACTOR_UNKNOWN)
    yield
    actor.reset(token)


@pytest.fixture(autouse=True)
def reset_rate_limiters() -> Any:
    """Leegt de emmers van de limiters tussen tests.

    De faaltellers en de grove grens staan op moduleniveau, dus zonder dit telt een test
    de mislukte authenticatiepogingen van de vorige mee en krijgt hij een 429 waar hij een
    401 toetst. Het aantal tests dat met een foute sleutel klopt ligt boven de grens.
    """
    from opi.api.rate_limiter import API_FLOOD_LIMITER, AUTH_FAILURE_PER_CLIENT, AUTH_FAILURE_PER_KEY

    limiters = (AUTH_FAILURE_PER_CLIENT, AUTH_FAILURE_PER_KEY, API_FLOOD_LIMITER)

    def leeg() -> None:
        for limiter in limiters:
            limiter._tokens.clear()
            limiter._last_update.clear()
            limiter._ip_tokens.clear()
            limiter._ip_last_update.clear()
            limiter._new_client_timestamps.clear()

    leeg()
    yield
    leeg()


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
#: Prefix van de database per run: ``zadtest_<namespace>_<pid>_<epoch>``. De pid maakt een
#: verweesde database herkenbaar, de namespace zegt of we die pid mogen geloven.
#: Bewust niet ``zad_test_``: de veeg van oudere takken leest die naam als ``zad_test_<pid>``,
#: crasht op deze vorm in ``os.kill`` en geeft alle ORM-tests van die run een ERROR.
ZAD_TEST_DB_PREFIX = "zadtest_"
#: Een database uit een VREEMDE pid-namespace mag pas weg als geen enkele run nog zo lang
#: kan draaien. De volledige suite doet zo'n 27 minuten.
ZAD_TEST_DB_MAX_LEEFTIJD_S = 12 * 3600


class TestPostgresError(DockerError):
    """De test-Postgres is er niet en kan er niet komen. Luid falen, nooit stil."""


_docker = partial(docker, fout=TestPostgresError)


def _container_staat() -> tuple[bool, str]:
    """(draait hij, op welk image). Bestaat hij niet, dan (False, "")."""
    return container_state(ZAD_TEST_PG_CONTAINER, fout=TestPostgresError)


def _wacht_tot_hij_luistert(seconden: int = 60) -> None:
    """Wacht tot de ECHTE server luistert, niet tot de init-server van de entrypoint.

    De toets gaat over TCP (``-h 127.0.0.1``) en niet over de unix-socket. Dat is het hele
    punt: bij een koude start zet de postgres-entrypoint eerst een tijdelijke server op om
    initdb en de init-scripts te draaien, en die start hij met ``listen_addresses=''``, dus
    alleen op de socket. Een ``pg_isready`` zonder host slaagt daar al tegen, waarna de
    entrypoint hem stopt en de echte server start. In dat gat verdwijnt de socket, en de
    eerste query erna valt erin met "No such file or directory". Dat kostte op een koude
    runner alle ORM-tests in een keer, en viel lokaal nooit op omdat de container daar al
    warm stond.
    """
    import time

    einde = time.monotonic() + seconden
    while time.monotonic() < einde:
        klaar = _docker("exec", ZAD_TEST_PG_CONTAINER, "pg_isready", "-U", "postgres", "-h", "127.0.0.1", check=False)
        if klaar.returncode == 0:
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


def _pid_namespace() -> str:
    """Alleen binnen deze namespace zegt ``os.kill`` iets over een pid."""
    try:
        return str(os.stat("/proc/self/ns/pid").st_ino)
    except OSError:
        return "0"


def _is_wees(naam: str, nu: float) -> bool:
    """Bij twijfel nee: een wees kost niets, een weggehaalde levende database alle ORM-tests.

    Databases met het oude prefix ``zad_test_`` ziet de veeg niet; die haalt
    ``task test-db-reset`` weg.
    """
    namespace, _, rest = naam.removeprefix(ZAD_TEST_DB_PREFIX).partition("_")
    pid, _, gemaakt = rest.partition("_")
    if not pid or not gemaakt:
        return False
    if namespace == _pid_namespace():
        return not _maker_leeft(pid)
    try:
        return nu - float(gemaakt) > ZAD_TEST_DB_MAX_LEEFTIJD_S
    except ValueError:
        return False


def _ruim_verweesde_databases_op() -> None:
    """Weg met de databases van runs die niet meer draaien.

    Hergebruik van pids kan een wees even laten staan; die valt bij een volgende run
    alsnog om. Een database van een LEVENDE run blijft staan, en dat is niet vrijblijvend:
    hier draaien suites naast elkaar (agents in eigen worktrees en in eigen containers), en
    dat is precies waarom elke run zijn eigen database heeft in plaats van zijn eigen
    container.
    """
    nu = time.time()
    namen = _psql(f"SELECT datname FROM pg_database WHERE datname LIKE '{ZAD_TEST_DB_PREFIX}%'")
    for naam in namen.splitlines():
        naam = naam.strip()
        if naam and _is_wees(naam, nu):
            _psql(f'DROP DATABASE IF EXISTS "{naam}" WITH (FORCE)')


# --- Het harnas voor een migratietoets ------------------------------------------------
#
# Een migratie toets je op een LEGE database met ``alembic upgrade`` erover, en niet op de
# database van ``orm_db``: die krijgt zijn tabellen uit de modellen en zou de migratie
# juist overslaan. Dit staat hier omdat meer dan een migratietoets het nodig heeft.

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "opi" / "migrations"))
    return config


def alembic_script_directory() -> ScriptDirectory:
    """De migratieketen, los van een database. Voor een toets op de revisies zelf."""
    return ScriptDirectory.from_config(_alembic_config())


def _alembic_run(engine: Engine, destination: str, omhoog: bool) -> None:
    """``alembic upgrade`` of ``downgrade`` op deze engine, zonder de URL uit env.py."""
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)

    def steps(rev, _context):
        revs = script._upgrade_revs if omhoog else script._downgrade_revs
        return revs(destination, rev)

    with (
        engine.connect() as connection,
        EnvironmentContext(config, script, fn=steps, destination_rev=destination) as env,
    ):
        env.configure(connection=connection, target_metadata=Base.metadata)
        with env.begin_transaction():
            env.run_migrations()
        connection.commit()


def alembic_upgrade(engine: Engine, destination: str) -> None:
    """``alembic upgrade <destination>``."""
    _alembic_run(engine, destination, omhoog=True)


def alembic_downgrade(engine: Engine, destination: str) -> None:
    """``alembic downgrade <destination>``, bijvoorbeeld ``-1``."""
    _alembic_run(engine, destination, omhoog=False)


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    """An EMPTY database next to the one of this run, as a sync (psycopg2) URL."""
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    # Eigen prefix: de weesopruimer hierboven leest een pid uit zad_test_<pid> en zou een
    # naam met achtervoegsel als dood opruimen terwijl deze run hem gebruikt.
    name = f"zad_mig_{os.getpid()}"
    admin = create_engine(f"{server}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield f"{server}/{name}"
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def _orm_db_url() -> Iterator[str]:
    """De verbinding naar de database van DEZE run, in de gedeelde server."""
    poort = _zorg_voor_container()
    _ruim_verweesde_databases_op()

    naam = f"{ZAD_TEST_DB_PREFIX}{_pid_namespace()}_{os.getpid()}_{int(time.time())}"
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
def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Keep only this shard's files under TEST_SHARDS/TEST_SHARD, and remember how many tests
    will run, so every progress line can show 'n of total'.

    Per file and not per test: module scoped fixtures then stay in one run. Done here and not
    with a file list in the CI step, so that step still calls ``tests/`` and
    test_rotation_ci_wiring can keep reading it.

    ``trylast``, because marker deselection (``-m e2e``) also happens in this hook: counting
    earlier shows 9054 as the total while 462 tests run.
    """
    shards = int(os.environ.get("TEST_SHARDS", "1"))
    if shards > 1:
        shard = int(os.environ["TEST_SHARD"])
        files = sorted({item.path for item in items})
        mine = {path for index, path in enumerate(files) if index % shards == shard}
        config.hook.pytest_deselected(items=[item for item in items if item.path not in mine])
        items[:] = [item for item in items if item.path in mine]
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


PLATFORMBEHEERDER_EMAIL = "beheerder@example.test"


@pytest.fixture
def platformbeheerder_token() -> Iterator[dict[str, Any]]:
    """Laat ``validate_platform_admin_token`` een vast e-mailadres aannemen.

    De tokenverificatie zelf staat in ``tests/test_user_token_auth.py``; een route achter
    de decorator heeft alleen de uitkomst nodig. ``headers`` legitimeert een
    platformbeheerder, ``headers_gewone_gebruiker`` een ingelogde niet-beheerder.
    """
    claims = {
        "sub": "beheerder-1",
        "sid": "sso-sessie-1",
        "email": PLATFORMBEHEERDER_EMAIL,
        "email_verified": True,
        "preferred_username": "beheerder",
    }

    def _claims_van(token: str) -> dict[str, Any]:
        return claims if token == "beheerder-token" else {**claims, "email": "iemand@example.test"}

    with (
        patch("opi.api.user_token_auth.verify_user_token", AsyncMock(side_effect=_claims_van)),
        patch("opi.api.user_token_auth.get_user_service") as get_service,
    ):
        get_service.return_value.is_email_allowed.return_value = True
        get_service.return_value.is_platform_admin.side_effect = lambda email: email == PLATFORMBEHEERDER_EMAIL
        yield {
            "email": PLATFORMBEHEERDER_EMAIL,
            "headers": {"Authorization": "Bearer beheerder-token"},
            "headers_gewone_gebruiker": {"Authorization": "Bearer gewone-gebruiker-token"},
        }


#: De enige code die :func:`verse_otp` aanneemt, en maar een keer.
OTP_CODE = "123456"


@pytest.fixture
def verse_otp() -> Iterator[dict[str, Any]]:
    """Laat de step-up van de API een code eenmalig aannemen.

    De OTP-dienst zelf staat in ``tests/test_user_otp_service.py``; een route achter
    ``requires_step_up`` heeft alleen de uitkomst nodig. Een tweede aanroep met dezelfde
    code is ongeldig, zoals bij een echte authenticator.
    """
    gebruikt: set[tuple[str, str]] = set()

    async def _verify(email: str, code: str) -> OtpVerification:
        if code != OTP_CODE or (email, code) in gebruikt:
            return OtpVerification.INVALID
        gebruikt.add((email, code))
        return OtpVerification.OK

    service = MagicMock(
        verify=AsyncMock(side_effect=_verify),
        status=AsyncMock(return_value=OtpStatus(registered=True)),
    )
    with (
        patch("opi.api.otp_step_up.UserOtpService", return_value=service),
        patch("opi.services.api_step_up_service.ApiStepUpService.is_fresh", AsyncMock(return_value=True)),
    ):
        yield {"headers": {}, "code": OTP_CODE}
