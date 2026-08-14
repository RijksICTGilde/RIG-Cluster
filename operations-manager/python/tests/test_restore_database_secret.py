"""A versioned database restore must leave working credentials behind.

The restore rotates the password of the database user and moves the data into a new
generation of the database. That password is known nowhere else, so if the restore does
not write it into the deployment's database secret, the secret keeps pointing at the old
database with a password that no longer authenticates.

The damage is not limited to the restored deployment. The project refresh that runs
right after the restore reads that secret, tests it, finds it broken and refuses to
touch a secret it did not author ("Manual intervention required to fix database user or
update secret"). It aborts before writing any manifests -- and so does every later
change to the project. A restore reported ``success`` and left the project on a lock.

These tests pin both halves:

* the secret carries the credentials the restore actually created;
* a change made after the restore still resolves those credentials instead of raising.
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
# Username and generation-0 database name are the same string by construction, which is
# exactly why the secret cannot be repaired with a blind search-and-replace.
USERNAME = "e2e62_glv_productie"
OLD_DATABASE = "e2e62_glv_productie"
NEW_DATABASE = "e2e62_glv_productie_v1"
OLD_PASSWORD = "OldPassword123old"
SECRET_NAME = f"{DEPLOYMENT}-database"


class _FakePostgresServer:
    """Just enough of a PostgreSQL server to tell a live password from a stale one."""

    def __init__(self) -> None:
        self.username = USERNAME
        self.live_password = OLD_PASSWORD
        self.databases = {OLD_DATABASE}

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
        return {"status": "created"}

    async def update_user_password(self, username: str, new_password: str) -> dict[str, str]:
        self.server.live_password = new_password
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
    """An in-memory stand-in for the two secret calls the restore makes."""

    def __init__(self, secrets: dict[tuple[str, str], dict[str, str]]) -> None:
        self.secrets = secrets

    async def get_secret(self, secret_name: str, namespace: str) -> dict[str, str] | None:
        stored = self.secrets.get((secret_name, namespace))
        return dict(stored) if stored is not None else None

    async def patch_secret_data(self, secret_name: str, namespace: str, data: dict[str, str]) -> None:
        if (secret_name, namespace) not in self.secrets:
            raise AssertionError(f"patch of a non-existent secret {secret_name} in {namespace}")
        self.secrets[(secret_name, namespace)].update(data)


def _live_secret_data() -> dict[str, str]:
    """The secret as it stands before the restore: generation 0, the then-current password."""
    return DatabaseSecret(
        host=HOST,
        port=5432,
        username=USERNAME,
        password=OLD_PASSWORD,
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
def secrets() -> dict[tuple[str, str], dict[str, str]]:
    return {(SECRET_NAME, NAMESPACE): _live_secret_data()}


@pytest.fixture
def kubectl(secrets: dict[tuple[str, str], dict[str, str]]) -> _FakeKubectlConnector:
    return _FakeKubectlConnector(secrets)


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


async def _run_restore(project_data: dict[str, Any] | None = None) -> dict[str, Any]:
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
        project_data=project_data if project_data is not None else _project_data(),
        project_file_handler=project_file_handler,
    )


class TestRestoredCredentialsLandInTheSecret:
    @pytest.mark.asyncio
    async def test_secret_carries_the_password_the_restore_set(
        self,
        server: _FakePostgresServer,
        secrets: dict[tuple[str, str], dict[str, str]],
        restore_environment: Any,
    ) -> None:
        result = await _run_restore()
        assert result["success"] is True

        stored = secrets[(SECRET_NAME, NAMESPACE)]
        # Not "different from the old one" but "the one that authenticates": the check
        # that a text comparison against the old value would have passed regardless.
        assert stored["DATABASE_PASSWORD"] == server.live_password
        assert server.live_password != OLD_PASSWORD
        assert server.authenticates(USERNAME, stored["DATABASE_PASSWORD"], stored["DATABASE_DB"])

    @pytest.mark.asyncio
    async def test_secret_points_at_the_database_the_restore_filled(
        self, secrets: dict[tuple[str, str], dict[str, str]], restore_environment: Any
    ) -> None:
        await _run_restore()

        stored = secrets[(SECRET_NAME, NAMESPACE)]
        assert stored["DATABASE_DB"] == NEW_DATABASE
        assert stored["DATABASE_SCHEMA"] == NEW_DATABASE
        assert NEW_DATABASE in stored["DATABASE_SERVER_FULL"]
        assert stored["DATABASE_PASSWORD"] in stored["DATABASE_SERVER_FULL"]

    @pytest.mark.asyncio
    async def test_read_only_role_is_left_alone(
        self, secrets: dict[tuple[str, str], dict[str, str]], restore_environment: Any
    ) -> None:
        """The restore rotated the main user, not the read-only one; that credential
        must survive so the refresh can re-grant it instead of re-issuing it."""
        await _run_restore()

        stored = secrets[(SECRET_NAME, NAMESPACE)]
        assert stored["DATABASE_SERVER_USER_RO"] == f"{USERNAME}_ro"
        assert stored["DATABASE_PASSWORD_RO"] == "ReadOnlyPassword123"

    @pytest.mark.asyncio
    async def test_absent_secret_does_not_fail_the_restore(
        self, secrets: dict[tuple[str, str], dict[str, str]], restore_environment: Any
    ) -> None:
        """A deployment that was never provisioned has no secret yet; the refresh
        creates it. That must not turn a successful restore into a failure."""
        secrets.clear()

        result = await _run_restore()
        assert result["success"] is True


class TestChangeAfterRestoreStillWorks:
    """The assertion that matters most: what the user hits is not the restore itself
    but everything after it."""

    @pytest.mark.asyncio
    async def test_credential_resolution_after_restore_does_not_demand_intervention(
        self,
        server: _FakePostgresServer,
        secrets: dict[tuple[str, str], dict[str, str]],
        restore_environment: Any,
    ) -> None:
        await _run_restore()

        # This is what the project refresh -- and every later change to the project --
        # does first: read the secret, test it, and give up on the whole project if it
        # does not work.
        manager = DatabaseManager(
            project_manager=MagicMock(), db_host=HOST, admin_username="admin", admin_password="admin"
        )
        manager._postgres_connector = _FakePostgresConnector(server)  # type: ignore[assignment]

        stored = DatabaseSecret.from_k8s_secret_data(secrets[(SECRET_NAME, NAMESPACE)])

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

        assert password == server.live_password
