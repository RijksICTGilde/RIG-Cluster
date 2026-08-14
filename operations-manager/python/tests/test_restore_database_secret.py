"""Een versie-restore mag de inloggegevens van de deployment niet ongeldig maken.

De restore maakte een nieuwe generatie van de database en **roteerde daarbij het
wachtwoord van de databasegebruiker**. Dat wachtwoord staat in een geheim dat ArgoCD
beheert -- het manifest ligt in ``zad-deployments`` en de applicatie synchroniseert met
``selfHeal: true`` -- dus het nieuwe wachtwoord kan daar alleen via de projectrefresh in
komen. En juist die refresh leest het geheim eerst, toetst het, en weigert door te gaan
als de inloggegevens niet werken (``Manual intervention required to fix database user or
update secret``). Gevolg: de restore meldde ``success``, de refresh brak af vóór er ook
maar één manifest geschreven was, en elke volgende wijziging op het project liep op
dezelfde melding vast.

Het wachtwoord roteren levert hier niets op: de gebruiker bestaat al en wordt gewoon ook
eigenaar van de nieuwe generatie. Deze tests leggen dat vast:

* de restore laat het wachtwoord staan dat de deployment al heeft;
* alleen zonder geheim om uit te lezen wordt er een gemaakt en gezet;
* een wijziging ná de restore lost de inloggegevens gewoon op in plaats van te
  struikelen -- dat is de fout die de gebruiker treft.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.restore_router import _restore_database_with_versioning
from opi.manager.database_manager import DatabaseManager
from opi.utils.secrets import DatabaseSecret

PROJECT = "e2e62-glv"
DEPLOYMENT = "productie"
NAMESPACE = "rig-e2e62-glv"
HOST = "postgresql.rig-system.svc.cluster.local"
USERNAME = "e2e62_glv_productie"
OLD_DATABASE = "e2e62_glv_productie"
NEW_DATABASE = "e2e62_glv_productie_v1"
LIVE_PASSWORD = "LivePassword123live"


class _FakePostgresServer:
    """Just enough of a PostgreSQL server to tell a live password from a stale one."""

    def __init__(self) -> None:
        self.username = USERNAME
        self.live_password = LIVE_PASSWORD
        self.databases = {OLD_DATABASE}
        self.password_writes: list[str] = []

    def authenticates(self, username: str, password: str, database: str) -> bool:
        return username == self.username and password == self.live_password and database in self.databases


class _FakePostgresConnector:
    def __init__(self, server: _FakePostgresServer) -> None:
        self.server = server

    async def create_user(self, username: str, password: str, database_privileges: Any = None) -> dict[str, str]:
        if username == self.server.username:
            return {"status": "exists"}
        self.server.username = username
        self.server.live_password = password
        self.server.password_writes.append(password)
        return {"status": "created"}

    async def update_user_password(self, username: str, new_password: str) -> dict[str, str]:
        self.server.live_password = new_password
        self.server.password_writes.append(new_password)
        return {"status": "success"}

    async def update_user_privileges(self, username: str, database_privileges: Any = None) -> dict[str, str]:
        return {"status": "success"}

    async def create_database(self, database_name: str, owner: str) -> dict[str, str]:
        self.server.databases.add(database_name)
        return {"status": "created"}

    async def create_schema(self, schema_name: str, database: str, owner: str) -> dict[str, str]:
        return {"status": "created"}

    async def close(self) -> None:
        return None


class _FakeKubectlConnector:
    """An in-memory stand-in for the secret read the restore does.

    It deliberately has no write method: a test that made the restore write to the live
    secret would fail with an AttributeError, which is the point -- ArgoCD owns that
    object and reverts a direct patch within milliseconds.
    """

    def __init__(self, secret_data: dict[str, str] | None) -> None:
        self.secret_data = secret_data
        self.reads: list[tuple[str, str]] = []

    async def get_secret(self, secret_name: str, namespace: str) -> dict[str, str] | None:
        self.reads.append((secret_name, namespace))
        return dict(self.secret_data) if self.secret_data is not None else None


def _live_secret_data() -> dict[str, str]:
    """The secret as it stands before the restore: generation 0, the live password."""
    return DatabaseSecret(
        host=HOST,
        port=5432,
        username=USERNAME,
        password=LIVE_PASSWORD,
        database=OLD_DATABASE,
        schema=OLD_DATABASE,
        ro_username=f"{USERNAME}_ro",
        ro_password="ReadOnlyPassword123",
    ).to_k8s_secret_data()


def _project_data() -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": PROJECT,
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "components": [{"name": "web", "type": "single", "services": ["postgresql-database"]}],
        "deployments": [
            {"name": DEPLOYMENT, "cluster": "local", "namespace": PROJECT, "components": [{"reference": "web"}]}
        ],
    }


@pytest.fixture
def server() -> _FakePostgresServer:
    return _FakePostgresServer()


@pytest.fixture
def kubectl() -> _FakeKubectlConnector:
    return _FakeKubectlConnector(_live_secret_data())


@pytest.fixture
def restore_environment(server: _FakePostgresServer, kubectl: _FakeKubectlConnector) -> Any:
    """Patch out everything the versioned database restore reaches for."""
    backup_manager = MagicMock()
    backup_manager.restore_database = AsyncMock(return_value=MagicMock(success=True, error=None))

    with (
        patch("opi.connectors.postgres.create_postgres_connector", return_value=_FakePostgresConnector(server)),
        patch("opi.core.cluster_config.get_database_server", return_value=HOST),
        patch("opi.api.restore_router.create_database_backup_manager", return_value=backup_manager),
        patch("opi.api.restore_router.create_kubectl_connector", return_value=kubectl),
    ):
        yield backup_manager


async def _run_restore() -> dict[str, Any]:
    project_file_handler = MagicMock()
    project_file_handler.get_database_generation = MagicMock(return_value=0)
    return await _restore_database_with_versioning(
        project_name=PROJECT,
        deployment_name=DEPLOYMENT,
        component_name="web",
        reference_name=f"{DEPLOYMENT}-database",
        snapshot_id="k1234567890abcdef",
        deployment_cluster="local",
        namespace=NAMESPACE,
        project_data=_project_data(),
        project_file_handler=project_file_handler,
    )


class TestDeRestoreLaatHetWachtwoordStaan:
    @pytest.mark.asyncio
    async def test_wachtwoord_wordt_niet_geroteerd(self, server: _FakePostgresServer, restore_environment: Any) -> None:
        result = await _run_restore()

        assert result["success"] is True
        assert result["new_resource_name"] == NEW_DATABASE
        assert server.live_password == LIVE_PASSWORD
        assert server.password_writes == [], f"de restore schreef alsnog een wachtwoord: {server.password_writes}"

    @pytest.mark.asyncio
    async def test_de_restorepod_krijgt_het_levende_wachtwoord(self, restore_environment: Any) -> None:
        """Het wachtwoord dat naar de restorepod gaat moet er wel een zijn dat werkt --
        anders komt de gegevens nooit in de nieuwe database terecht."""
        await _run_restore()

        call = restore_environment.restore_database.await_args.kwargs
        assert call["target_database_password"] == LIVE_PASSWORD
        assert call["target_database_name"] == NEW_DATABASE
        assert call["target_database_user"] == USERNAME

    @pytest.mark.asyncio
    async def test_zonder_geheim_wordt_er_wel_een_wachtwoord_gezet(
        self, server: _FakePostgresServer, kubectl: _FakeKubectlConnector, restore_environment: Any
    ) -> None:
        """Is er geen geheim om uit te lezen, dan is er ook geen wachtwoord om te hergebruiken;
        de restorepod moet ergens mee kunnen inloggen."""
        kubectl.secret_data = None

        result = await _run_restore()

        assert result["success"] is True
        assert server.password_writes, "zonder geheim moet de restore wel een wachtwoord zetten"
        assert server.live_password == server.password_writes[-1]


class TestWijzigingNaDeRestore:
    """De assertie die er het meest toe doet: wat de gebruiker treft is niet de restore
    zelf maar alles daarna."""

    @pytest.mark.asyncio
    async def test_inloggegevens_oplossen_vraagt_geen_handmatig_ingrijpen(
        self, server: _FakePostgresServer, kubectl: _FakeKubectlConnector, restore_environment: Any
    ) -> None:
        await _run_restore()

        # Dit is wat de projectrefresh -- en elke volgende wijziging op het project --
        # als eerste doet: het geheim lezen, het toetsen, en het hele project opgeven
        # als het niet werkt. Het geheim is ONVERANDERD (ArgoCD beheert het), maar wijst
        # nu naar de nieuwe generatie omdat de refresh die uit het projectbestand haalt.
        manager = DatabaseManager(
            project_manager=MagicMock(), db_host=HOST, admin_username="admin", admin_password="admin"
        )
        manager._postgres_connector = _FakePostgresConnector(server)  # type: ignore[assignment]

        stored = DatabaseSecret.from_k8s_secret_data(_live_secret_data())

        async def _test_connection(username: str, password: str, database: str, schema: str, host: str) -> bool:
            return server.authenticates(username, password, database)

        with (
            patch.object(
                DatabaseManager, "_get_existing_database_credentials_from_k8s", AsyncMock(return_value=stored)
            ),
            patch.object(DatabaseManager, "_test_database_connection", staticmethod(_test_connection)),
        ):
            password = await manager._resolve_database_credentials(
                project_name=PROJECT,
                deployment_name=DEPLOYMENT,
                deployment={"namespace": PROJECT},
                db_username=USERNAME,
                db_database=NEW_DATABASE,
                db_schema=NEW_DATABASE,
                db_host=HOST,
                admin_username="admin",
                admin_password="admin",
            )

        assert password == LIVE_PASSWORD
