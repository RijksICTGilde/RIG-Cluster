"""
Test server for UI development and E2E testing.

Starts the real FastAPI app with mocked external services and seeded
project data from local YAML files. No database, Git, Keycloak, or
Kubernetes cluster required.

Usage:
    # Interactive development (with hot-reload):
    cd operations-manager/python
    uv run python -m tests.e2e.testserver

    # From pytest (via app_server fixture):
    Used automatically by tests/e2e/conftest.py
"""

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

logger = logging.getLogger(__name__)

SECRET_KEY = "e2e-test-secret-key-padded-to-32-chars-minimum"

# Fixed test AGE keypair for E2E testing (DO NOT use in production)
TEST_AGE_PUBLIC_KEY = "age10uegg2n4sxnsmpd00xjqh8e80hhrs9983yhy673gp8k0aevn4dtsn9d8xj"
TEST_AGE_PRIVATE_KEY = "AGE-SECRET-KEY-1P9VAE6J5J7FK0LF2TH0FG7HMNS8XC9T4GTJQNGWRJAS40DYAGULQTCCAMK"

TEST_USER_EMAIL = "test@example.com"

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "projects"


class InMemoryUserAdminService:
    """In-memory stub for UserAdminService used in E2E tests.

    Replaces the database-backed service so that user admin pages can be
    tested without a running PostgreSQL instance.
    """

    def __init__(self) -> None:
        import uuid
        from datetime import UTC, datetime

        self._users: dict[str, dict] = {}
        # Seed a couple of users so the list page isn't empty
        for email, name in [
            ("jan@example.nl", "Jan de Vries"),
            ("maria@example.nl", "Maria Jansen"),
        ]:
            uid = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            self._users[uid] = {
                "id": uid,
                "email": email,
                "full_name": name,
                "created_at": now,
                "updated_at": now,
            }

    async def list_users(self) -> list[dict]:
        return sorted(self._users.values(), key=lambda u: u["full_name"])

    async def get_user(self, user_id: str) -> dict | None:
        return self._users.get(user_id)

    async def get_user_by_email(self, email: str) -> dict | None:
        for u in self._users.values():
            if u["email"] == email:
                return u
        return None

    async def create_user(self, email: str, full_name: str) -> dict:
        import uuid
        from datetime import UTC, datetime

        # Check uniqueness (raise same error as asyncpg would)
        for u in self._users.values():
            if u["email"] == email:
                from asyncpg import UniqueViolationError

                raise UniqueViolationError
        uid = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        user = {"id": uid, "email": email, "full_name": full_name, "created_at": now, "updated_at": now}
        self._users[uid] = user
        return user

    async def update_user(self, user_id: str, email: str, full_name: str) -> dict | None:
        from datetime import UTC, datetime

        if user_id not in self._users:
            return None
        # Check uniqueness against other users
        for uid, u in self._users.items():
            if u["email"] == email and uid != user_id:
                from asyncpg import UniqueViolationError

                raise UniqueViolationError
        self._users[user_id]["email"] = email
        self._users[user_id]["full_name"] = full_name
        self._users[user_id]["updated_at"] = datetime.now(UTC).isoformat()
        return self._users[user_id]

    async def delete_user(self, user_id: str) -> bool:
        return self._users.pop(user_id, None) is not None


# Singleton so the test server and tests share the same instance
_in_memory_user_service: InMemoryUserAdminService | None = None


def get_in_memory_user_service() -> InMemoryUserAdminService:
    """Get (or create) the shared in-memory user admin service."""
    global _in_memory_user_service
    if _in_memory_user_service is None:
        _in_memory_user_service = InMemoryUserAdminService()
    return _in_memory_user_service


def _mock_get_service() -> InMemoryUserAdminService:
    """Drop-in replacement for router_user_admin._get_service."""
    return get_in_memory_user_service()


#: Vaste metriekreeksen voor /admin/diensten, gesleuteld op de naam die de meetlaag
#: aan zijn queries geeft. De getallen zijn die van de meting tegen productie van
#: 18 augustus 2026, inclusief de PVC van 92,7% die niemand zag; zo toont de pagina in
#: de browsertest hetzelfde beeld als de aanleiding voor deze pagina.
_DIENSTEN_METRIEKEN: dict[str, list[dict]] = {
    "vulling": [
        {
            "metric": {"namespace": "rig-prd-ubbw-0i1", "persistentvolumeclaim": "production-typesense-data-pvc"},
            "value": [1787000000.0, "92.7"],
        },
        {
            "metric": {
                "namespace": "rig-prd-mb-docs-helmfile-infrastructure",
                "persistentvolumeclaim": "mb-docs-helmfile-db-1",
            },
            "value": [1787000000.0, "62.8"],
        },
        {
            "metric": {"namespace": "rig-prd-algor-odc-infrastructure", "persistentvolumeclaim": "algor-odc-db-1"},
            "value": [1787000000.0, "60.8"],
        },
        {
            "metric": {"namespace": "rig-prd-operations", "persistentvolumeclaim": "minio-storage-versioned"},
            "value": [1787000000.0, "40.5"],
        },
    ],
    "gebruikt": [
        {
            "metric": {"namespace": "rig-prd-ubbw-0i1", "persistentvolumeclaim": "production-typesense-data-pvc"},
            "value": [1787000000.0, "9955571302"],
        },
        {
            "metric": {
                "namespace": "rig-prd-mb-docs-helmfile-infrastructure",
                "persistentvolumeclaim": "mb-docs-helmfile-db-1",
            },
            "value": [1787000000.0, "674309865"],
        },
        {
            "metric": {"namespace": "rig-prd-algor-odc-infrastructure", "persistentvolumeclaim": "algor-odc-db-1"},
            "value": [1787000000.0, "652835225"],
        },
        {
            "metric": {"namespace": "rig-prd-operations", "persistentvolumeclaim": "minio-storage-versioned"},
            "value": [1787000000.0, "43486543872"],
        },
    ],
    "capaciteit": [
        {
            "metric": {"namespace": "rig-prd-ubbw-0i1", "persistentvolumeclaim": "production-typesense-data-pvc"},
            "value": [1787000000.0, "10737418240"],
        },
        {
            "metric": {
                "namespace": "rig-prd-mb-docs-helmfile-infrastructure",
                "persistentvolumeclaim": "mb-docs-helmfile-db-1",
            },
            "value": [1787000000.0, "1073741824"],
        },
        {
            "metric": {"namespace": "rig-prd-algor-odc-infrastructure", "persistentvolumeclaim": "algor-odc-db-1"},
            "value": [1787000000.0, "1073741824"],
        },
        {
            "metric": {"namespace": "rig-prd-operations", "persistentvolumeclaim": "minio-storage-versioned"},
            "value": [1787000000.0, "107374182400"],
        },
    ],
    "inodes": [
        {
            "metric": {"namespace": "rig-prd-ubbw-0i1", "persistentvolumeclaim": "production-typesense-data-pvc"},
            "value": [1787000000.0, "3.1"],
        },
    ],
    "grootte": [
        {
            "metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "forgejo"},
            "value": [1787000000.0, "24049331"],
        },
        {
            "metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "keycloak"},
            "value": [1787000000.0, "19166899"],
        },
    ],
    "verbindingen": [
        {"metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "forgejo"}, "value": [1787000000.0, "2"]},
        {"metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "keycloak"}, "value": [1787000000.0, "3"]},
    ],
    "langste_transactie": [
        {"metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "forgejo"}, "value": [1787000000.0, "0"]},
        {"metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "keycloak"}, "value": [1787000000.0, "0"]},
    ],
    "xid_leeftijd": [
        {
            "metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "forgejo"},
            "value": [1787000000.0, "71449"],
        },
        {
            "metric": {"namespace": "rig-system", "pod": "rig-db-1", "datname": "keycloak"},
            "value": [1787000000.0, "71449"],
        },
    ],
    "wachtend": [
        {"metric": {"namespace": "rig-system", "pod": "rig-db-1"}, "value": [1787000000.0, "0"]},
    ],
    # Het Keycloak-blok. Dit blok praat met opzet RECHTSTREEKS met PrometheusConnector
    # (de metrieken zitten niet in Mimir), dus het komt niet langs de metriekconnector
    # hierboven en heeft een eigen stand-in nodig - zie _fake_prometheus_connector.
    "realms": [
        {"metric": {}, "value": [1787000000.0, "7"]},
    ],
    "gebruikers": [
        {"metric": {"realm": "master"}, "value": [1787000000.0, "3"]},
        {"metric": {"realm": "algor-odc-odcn-production"}, "value": [1787000000.0, "12"]},
    ],
    "gebruikers_per_idp": [
        {"metric": {"realm": "algor-odc-odcn-production", "idp_type": "rijksportaal"}, "value": [1787000000.0, "9"]},
        {"metric": {"realm": "algor-odc-odcn-production", "idp_type": "lokaal"}, "value": [1787000000.0, "3"]},
    ],
    "logins": [
        {"metric": {"realm": "algor-odc-odcn-production"}, "value": [1787000000.0, "24"]},
    ],
    "mislukte_logins": [
        {"metric": {"realm": "algor-odc-odcn-production"}, "value": [1787000000.0, "1"]},
    ],
}


def _fake_metrics_connector():
    """Een metriekconnector met vaste antwoorden, voor /admin/diensten.

    Zonder deze zou elke browsertest van die pagina alleen "kon niet meten" te zien
    krijgen - waar en heel, maar dan zie je de tabellen nooit. De opzoeking gaat via de
    QUERYTEKST: verandert een query zonder dat hier een antwoord bij komt, dan valt dat
    blok leeg en zegt de test dat.
    """
    from unittest.mock import AsyncMock

    from opi.services.gedeelde_diensten import _DATABASE_QUERIES, _KEYCLOAK_QUERIES, _OPSLAG_QUERIES

    op_query = {query: naam for naam, query in {**_OPSLAG_QUERIES, **_DATABASE_QUERIES, **_KEYCLOAK_QUERIES}.items()}

    async def custom_query(query: str) -> list[dict]:
        return _DIENSTEN_METRIEKEN.get(op_query.get(query, ""), [])

    connector = MagicMock()
    connector.custom_query = AsyncMock(side_effect=custom_query)
    return connector


def _fake_prometheus_connector():
    """Een stand-in voor PrometheusConnector zelf, voor het Keycloak-blok.

    Het Keycloak-blok van /admin/diensten gaat met opzet NIET langs
    ``get_metrics_connector()`` -- die metrieken staan alleen in onze eigen Prometheus -
    en bouwt zijn eigen ``PrometheusConnector()``. Dat is een echte HTTP-client, dus in
    deze harnas ging het blok het netwerk op en wachtte het de volledige DNS- en
    retryketen af op een naam die hier niet bestaat.

    Dat kostte niet alleen dat blok. Zolang die keten liep bleven ook de twee andere,
    wel gestubde blokken op "wordt opgehaald..." staan; zeven browsertests stonden
    daarop rood. De blokkade zelf is in de connector gerepareerd (asyncio.to_thread), en
    hier wordt de netwerkaanroep vervangen - een standalone suite hoort geen naam op te
    zoeken.
    """
    return _fake_metrics_connector()


async def _fake_store_save(
    self,
    name: str,
    data: dict,
    *,
    message: str,
    actor: str,
    enforce_validation: bool = True,
    filename: str | None = None,
    refresh_cache: bool = True,
    base: dict | None = None,
):
    """In-memory stand-in for GitProjectStore.save.

    Every project-file write goes through the store, which clones the real
    zad-projects repo - unavailable here. This keeps the store's write-through
    cache update (so save-then-read round trips behave like production) and
    skips only the git commit/push.
    """
    from opi.services.project_store import MutationResult

    resolved = os.path.basename(filename or f"{name}.yaml")
    if refresh_cache:
        self._refresh_cache(name, data, resolved)
    return MutationResult(before=None, after=data, ref="e2e-testserver")


def _load_fixture_projects() -> list[dict]:
    """Load all YAML project files from the fixtures directory."""
    projects = []
    if not FIXTURE_DIR.exists():
        logger.warning("Fixture directory does not exist: %s", FIXTURE_DIR)
        return projects

    for yaml_file in sorted(FIXTURE_DIR.glob("*.yaml")):
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
        if data and isinstance(data, dict) and "name" in data:
            projects.append(data)
            logger.info("Loaded fixture project: %s from %s", data["name"], yaml_file.name)
    return projects


def _seed_projects(projects: list[dict]) -> None:
    """Register fixture projects into ProjectService and UserService."""
    from opi.services.project_service import get_project_service, initialize_project_service
    from opi.services.user_service import get_user_service

    initialize_project_service()
    project_service = get_project_service()
    user_service = get_user_service()

    # Add test user to allowlist and grant platform-admin access
    user_service.add_platform_admins([TEST_USER_EMAIL])

    for project_data in projects:
        project_name = project_data["name"]
        config = project_data.get("config", {})
        api_key = config.get("api-key", f"test-key-{project_name}")
        filename = f"{project_name}.yaml"
        users = project_data.get("users", [])

        project_service.register(project_name, str(api_key), filename, users, project_data)

        # Add project user emails to allowlist
        project_emails = [u.get("email") for u in users if u.get("email")]
        if project_emails:
            user_service.add_allowed_emails(project_emails)

    logger.info("Seeded %d fixture projects", len(projects))


def _preinitialize_kubectl_without_probing() -> None:
    """Mark the kubectl singleton as initialised so nothing ever probes a cluster.

    ``KubectlConnector.__init__`` runs a BLOCKING
    ``subprocess.run(["kubectl", "auth", "whoami"], timeout=10)`` on whatever thread
    first constructs it -- here the uvicorn event loop that serves every request. There
    is no cluster in a local E2E run, so on a machine that HAS a kubectl binary (a dev
    box with kind, and the shared dev server) the probe does not fail fast: it hangs for
    the full 10 seconds, and every request in flight waits behind it.

    That is what makes this suite look order-dependent when it is not. The connector is
    a process-wide singleton, so the stall happens exactly once per run, on whichever
    test happens to touch a page that constructs it -- a different test in every shuffle,
    and the neighbouring test's own 10s wait expires with it. Measured: run with seed 404
    lost ``test_saves_description_change`` (its step POST timed out at exactly 10s, with
    ``Error testing kubectl connection ... timed out after 10 seconds`` alongside it) and
    then ``test_detail_page_renders``, which reads the project that test no longer got to
    restore. Three other seeds, where the probe never ran, were green.

    Pre-building the singleton here skips the probe and the retry task it schedules.
    ``isConnected`` stays False, which is what the probe concluded anyway -- so no route
    behaves differently, there is just no 10-second hole in the event loop.
    """
    from opi.connectors.kubectl import KubectlConnector

    connector = KubectlConnector.__new__(KubectlConnector)
    connector.env = os.environ.copy()
    connector._initialized = True
    KubectlConnector._instance = connector
    KubectlConnector.isConnected = False
    KubectlConnector._retry_task = None


def create_test_app():
    """Create the FastAPI app with mocked externals and seeded test data.

    Returns a tuple of (app, patches_context) where patches_context is a
    contextmanager that must remain active while the app is running.
    """
    import contextlib

    @contextlib.contextmanager
    def patched_app():
        with (
            # Patch where het GEBRUIKT wordt (opi.server) en niet waar het gedefinieerd
            # is. ``opi/server.py`` doet ``from opi.core.startup import run_startup_tasks``
            # en roept die eigen naam aan; het definitiepad patchen raakt die binding
            # alleen als opi.server hieronder voor het EERST geimporteerd wordt.
            #
            # Dat maakte de suite afhankelijk van wat er verder verzameld werd: bij
            # ``pytest tests/e2e`` was opi.server nog niet geladen en pakte de import de
            # mock op; bij ``pytest -m e2e`` (de hele boom) had een unittest opi.server al
            # geimporteerd, bleef de ECHTE functie staan, en ging de app bij het opstarten
            # een database zoeken die er niet is. De retry duurt langer dan de 10s die de
            # ``app_server``-fixture wacht, dus faalde elke E2E-test in setup: 397 errors
            # op een suite die per bestand groen was.
            patch("opi.server.run_startup_tasks", new_callable=AsyncMock),
            patch("opi.core.config.settings.SECRET_KEY", SECRET_KEY),
            patch("opi.core.config.settings.ENABLE_GIT_MONITOR", False),
            patch(
                "opi.services.persistence.subdomain_registry.SubdomainConnector.get_by_subdomain",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", TEST_AGE_PRIVATE_KEY),
            # The wizard-create generators encrypt the project's AGE private key and
            # API key with SOPS_AGE_PUBLIC_KEY; without it, creating a project raises
            # "Missing public age key for encryption". The matching public key is
            # already defined above but was never wired to settings.
            patch("opi.core.config.settings.SOPS_AGE_PUBLIC_KEY", TEST_AGE_PUBLIC_KEY),
            patch(
                "opi.connectors.prometheus.get_metrics_connector",
                return_value=SimpleNamespace(is_connected=False),
            ),
            patch(
                "opi.connectors.argo.create_argo_connector",
                return_value=MagicMock(auth_token=None),
            ),
            patch("opi.handlers.project_file_handler.save_project_file"),
            patch("opi.services.project_store.GitProjectStore.save", _fake_store_save),
            # version_of() clones the real zad-projects repo to read a blob SHA -
            # unavailable here, same reason save is faked above. The edit-modal init
            # records this as the compare-and-swap base_version, so without a stand-in
            # every edit-modal open would fail decrypting the git creds. Return a fixed
            # valid blob SHA (40 hex chars, per _BLOB_SHA_RE); the faked save ignores it.
            patch(
                "opi.services.project_store.GitProjectStore.version_of",
                new_callable=AsyncMock,
                return_value="0" * 40,
            ),
            patch("opi.web.router_user_admin._get_service", _mock_get_service),
            # /admin/diensten leest via de metriekconnector; hier is geen Prometheus.
            patch(
                "opi.services.gedeelde_diensten.get_metrics_connector",
                new_callable=AsyncMock,
                return_value=_fake_metrics_connector(),
            ),
            # ... behalve het Keycloak-blok, dat rechtstreeks een PrometheusConnector
            # bouwt. Zie _fake_prometheus_connector.
            patch(
                "opi.connectors.prometheus.PrometheusConnector",
                return_value=_fake_prometheus_connector(),
            ),
            patch(
                "opi.manager.backup.BackupManager",
                return_value=MagicMock(
                    list_snapshots=AsyncMock(return_value=[]),
                ),
            ),
        ):
            _preinitialize_kubectl_without_probing()

            # Mark all readiness services as ready
            import opi.core.readiness as readiness_module

            readiness_module._state = None
            state = readiness_module.get_readiness_state()
            state.database.mark_ready()
            state.keycloak.mark_ready()
            state.oauth.mark_ready()
            state.projects.mark_ready()

            from opi.server import create_app

            app = create_app()

            # Seed fixture projects
            projects = _load_fixture_projects()
            _seed_projects(projects)

            yield app

    return patched_app


def run_standalone() -> None:
    """Run the test server standalone for interactive UI development."""
    import uvicorn

    port = int(os.environ.get("TEST_SERVER_PORT", "8111"))

    # Disable OIDC for standalone mode - no login needed
    os.environ.setdefault("OIDC_DISABLED", "true")

    print(f"""
========================================
  UI Test Server
========================================
  URL:     http://127.0.0.1:{port}
  Wizard:  http://127.0.0.1:{port}/forms/wizard/start
  Reload:  watching opi/ directory
  Auth:    disabled (OIDC_DISABLED=true)
========================================
""")

    ctx = create_test_app()
    with ctx() as app:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            reload=False,  # reload doesn't work well with patched app
        )
        server = uvicorn.Server(config)
        server.run()


if __name__ == "__main__":
    run_standalone()
