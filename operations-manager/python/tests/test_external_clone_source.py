"""Een externe kloonbron moet een publiek adres zijn, en zijn wachtwoord blijft uit de takentabel.

De guard zelf, de allowlist per cluster, de weigering in beide kloonwegen, de vorm van de
payload tussen route en handler, en de ene foutmelding naar buiten.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.core.cluster_config import CLUSTER_CONFIG, get_external_clone_allowed_private_networks
from opi.core.task_handlers_operations import handle_clone_bucket, handle_clone_database
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore
from opi.utils.egress_guard import (
    REFUSED_CLONE_SOURCE_REASON,
    UNREACHABLE_SOURCE_REASON,
    clone_source_refusal,
    host_is_allowed,
    host_is_public,
)
from opi.utils.sops import generate_sops_key_pair

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
API_KEY = "test-api-key-12345"
HEADERS = {"X-API-Key": API_KEY}

#: Welke host naar welke adressen resolvet; alles wat er niet in staat resolvet niet.
_ADDRESSES = {
    "db.publiek.nl": ["93.184.216.34"],
    "minio.publiek.nl": ["93.184.216.35"],
    "chisel.publiek.nl": ["93.184.216.36"],
    "chisel.intern": ["10.1.2.3"],
    "bron.intern": ["10.1.2.4"],
    "rig-db-rw.rig-system.svc.cluster.local": ["10.43.0.9"],
    "half.publiek.nl": ["93.184.216.37", "10.1.2.5"],
    "localhost": ["127.0.0.1"],
    "10.20.1.5": ["10.20.1.5"],
    "10.30.1.5": ["10.30.1.5"],
    "100.64.0.1": ["100.64.0.1"],
    "127.0.0.1": ["127.0.0.1"],
    "93.184.216.34": ["93.184.216.34"],
}


def _fake_getaddrinfo(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
    if host not in _ADDRESSES:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in _ADDRESSES[host]]


@pytest.fixture(autouse=True)
def resolver() -> Iterator[None]:
    """Geen echte DNS in een test: de guard beslist op deze tabel."""
    with patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo):
        yield


# ---------------------------------------------------------------------------
# De guard zelf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHostIsAllowed:
    @pytest.mark.parametrize("host", ["localhost", "bron.intern", "rig-db-rw.rig-system.svc.cluster.local"])
    async def test_a_private_host_is_refused_without_an_allowlist(self, host: str) -> None:
        assert await host_is_public(host) is False

    async def test_a_host_that_does_not_resolve_is_refused(self) -> None:
        assert await host_is_public("bestaat.niet.intern") is False

    async def test_a_public_host_is_allowed(self) -> None:
        assert await host_is_public("db.publiek.nl") is True

    async def test_one_private_address_among_public_ones_is_enough(self) -> None:
        assert await host_is_public("half.publiek.nl") is False

    async def test_shared_address_space_is_not_private_enough(self) -> None:
        """``is_private`` laat ``100.64.0.0/10`` door, ``is_global`` niet."""
        assert await host_is_public("100.64.0.1") is False

    async def test_an_allowed_network_opens_only_that_network(self) -> None:
        assert await host_is_allowed("10.20.1.5", ["10.20.0.0/16"]) is True
        assert await host_is_allowed("10.30.1.5", ["10.20.0.0/16"]) is False
        assert await host_is_allowed("127.0.0.1", ["10.20.0.0/16"]) is False


@pytest.mark.asyncio
class TestAnUnjudgeableSourceIsRefused:
    """Een opgegeven bron zonder bruikbare host mag de guard niet passeren.

    ``host: 0`` uit een projectbestand haalt het schema (``pattern`` geldt daar niet voor
    een integer) en komt bij ``asyncpg`` aan als de eigen loopback van de pod.
    """

    async def test_an_integer_host(self) -> None:
        assert await clone_source_refusal(0, None) == REFUSED_CLONE_SOURCE_REASON  # type: ignore[arg-type]

    async def test_a_server_url_without_a_host(self) -> None:
        assert await clone_source_refusal(None, "https://") == REFUSED_CLONE_SOURCE_REASON

    async def test_no_source_at_all_is_for_the_route_to_answer(self) -> None:
        assert await clone_source_refusal(None, None) is None


@pytest.mark.parametrize("cluster_name", sorted(CLUSTER_CONFIG))
def test_no_cluster_allows_a_private_network(cluster_name: str) -> None:
    assert get_external_clone_allowed_private_networks(cluster_name) == []


# ---------------------------------------------------------------------------
# De routes
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_task_service() -> AsyncMock:
    service = AsyncMock()
    service.create_task.return_value = {"task_id": SAMPLE_TASK_ID, "task_type": "clone_database", "status": "pending"}
    return service


@pytest.fixture
def mock_auth_project_service() -> Iterator[Any]:
    mock_service = MagicMock(spec=GitProjectStore)
    test_project = ProjectSummary(
        name="test-project",
        api_key=API_KEY,
        filename="test-project.yaml",
        users=[ProjectUser(email="user@example.com", role="Developer")],
    )
    mock_service.get = lambda name: test_project if name == "test-project" else None
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.v2.router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def age_keys() -> Iterator[tuple[str, str]]:
    """Een weggooipaar als clustersleutel, zodat de route echt versleutelt.

    Op de plek waar de route en de handler hem lezen: ``mock_settings`` legt een MagicMock
    over ``opi.core.config.settings``, en die reist mee naar wie hem al geïmporteerd had.
    """
    private_key, public_key = generate_sops_key_pair()
    with (
        patch("opi.api.router.settings.SOPS_AGE_PUBLIC_KEY", public_key),
        patch("opi.utils.age.settings.SOPS_AGE_PRIVATE_KEY", private_key),
    ):
        yield private_key, public_key


@pytest.fixture
def client(mock_settings: Any, mock_task_service: AsyncMock, mock_auth_project_service: Any) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


def _database_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "sourceHost": "db.publiek.nl",
        "sourcePort": 5432,
        "sourceUsername": "postgres",
        "sourcePassword": "geheim-wachtwoord",
        "sourceDatabase": "appdb",
        "sourceSchema": "app",
    }
    body.update(overrides)
    return body


def _bucket_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "sourceHost": "minio.publiek.nl",
        "sourcePort": 9000,
        "sourceAccessKey": "minioadmin",
        "sourceSecretKey": "geheime-sleutel",
        "sourceBucket": "data",
    }
    body.update(overrides)
    return body


_TUNNEL = {
    "serverUrl": "https://chisel.publiek.nl",
    "username": "admin",
    "password": "geheime-tunnel",
    "remoteHost": "bron.intern",
    "remotePort": 5432,
}

_REFUSED_HOSTS = ["localhost", "bron.intern", "100.64.0.1", "rig-db-rw.rig-system.svc.cluster.local", "bestaat.niet"]


class TestCloneRoutesRefuseANonPublicSource:
    @pytest.mark.parametrize("source_host", _REFUSED_HOSTS)
    def test_v2_clone_database(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str], source_host: str
    ) -> None:
        response = client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json=_database_body(sourceHost=source_host),
        )
        assert response.status_code == 422
        assert response.json()["detail"] == REFUSED_CLONE_SOURCE_REASON
        mock_task_service.create_task.assert_not_called()

    @pytest.mark.parametrize("source_host", _REFUSED_HOSTS)
    def test_v2_clone_bucket(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str], source_host: str
    ) -> None:
        response = client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            headers=HEADERS,
            json=_bucket_body(sourceHost=source_host),
        )
        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_v1_clone_database(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        response = client.post(
            "/api/projects/test-project/deployments/staging/:clone-database-from-external",
            headers=HEADERS,
            json=_database_body(sourceHost="bron.intern"),
        )
        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_a_tunnel_to_a_private_server_is_refused(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        body = _database_body(sourceHost=None, tunnel={**_TUNNEL, "serverUrl": "https://chisel.intern"})
        response = client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_a_tunnel_server_must_be_https(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        body = _database_body(sourceHost=None, tunnel={**_TUNNEL, "serverUrl": "http://chisel.publiek.nl"})
        response = client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_a_public_source_is_accepted(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        response = client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json=_database_body(),
        )
        assert response.status_code == 202
        mock_task_service.create_task.assert_called_once()

    def test_a_private_source_behind_a_public_tunnel_is_accepted(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        """``remoteHost`` resolvet aan de kant van de chisel-server, niet in ons cluster."""
        response = client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json=_database_body(sourceHost=None, tunnel=_TUNNEL),
        )
        assert response.status_code == 202


# ---------------------------------------------------------------------------
# Route naar handler: de vorm van de payload en de versleutelde geheimen
# ---------------------------------------------------------------------------


def _stored_payload(mock_task_service: AsyncMock) -> dict[str, Any]:
    return mock_task_service.create_task.call_args[1]["payload"]


class TestPayloadFromRouteToHandler:
    @pytest.mark.asyncio
    async def test_the_database_payload_reaches_the_handler(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json=_database_body(tunnel=_TUNNEL),
        )
        payload = _stored_payload(mock_task_service)

        project_manager = AsyncMock()
        project_manager.clone_database_from_external_with_tunnel = AsyncMock(
            return_value={"success": True, "source": {}, "target": {}, "operations": {"rows_copied": 3}}
        )
        with patch("opi.manager.project_manager.ProjectManager", return_value=project_manager):
            result = await handle_clone_database(payload, MagicMock())

        assert result["rows_copied"] == 3
        call = project_manager.clone_database_from_external_with_tunnel.call_args[1]
        assert call["source_password"] == "geheim-wachtwoord"
        assert call["tunnel_password"] == "geheime-tunnel"
        assert call["source_database"] == "appdb"

    @pytest.mark.asyncio
    async def test_the_bucket_payload_reaches_the_handler(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            headers=HEADERS,
            json=_bucket_body(),
        )
        payload = _stored_payload(mock_task_service)

        project_manager = AsyncMock()
        project_manager.clone_minio_bucket_from_external_direct = AsyncMock(
            return_value={"success": True, "source": {}, "target": {}, "operations": {"objects_copied": 7}}
        )
        with patch("opi.manager.project_manager.ProjectManager", return_value=project_manager):
            result = await handle_clone_bucket(payload, MagicMock())

        assert result["objects_copied"] == 7
        call = project_manager.clone_minio_bucket_from_external_direct.call_args[1]
        assert call["source_secret_key"] == "geheime-sleutel"
        assert call["source_bucket"] == "data"

    def test_the_stored_payload_carries_no_plain_secret(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json=_database_body(tunnel=_TUNNEL),
        )
        stored = str(_stored_payload(mock_task_service))
        assert "geheim-wachtwoord" not in stored
        assert "geheime-tunnel" not in stored

    def test_the_stored_bucket_payload_carries_no_plain_secret(
        self, client: TestClient, mock_task_service: AsyncMock, age_keys: tuple[str, str]
    ) -> None:
        client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            headers=HEADERS,
            json=_bucket_body(),
        )
        stored = str(_stored_payload(mock_task_service))
        assert "geheime-sleutel" not in stored
        assert "minioadmin" in stored, "de access key is geen geheim en blijft leesbaar"


# ---------------------------------------------------------------------------
# De gedeelde plek: ook de weg via remote-sources in het projectbestand
# ---------------------------------------------------------------------------


@pytest.fixture
def database_manager() -> Any:
    from opi.manager.database_manager import DatabaseManager

    return DatabaseManager(MagicMock(), "rig-db-rw", "admin", "geheim-admin")


@pytest.fixture
def minio_manager() -> Any:
    from opi.manager.minio_manager import MinioManager

    return MinioManager(MagicMock())


@pytest.mark.asyncio
class TestTheSharedCloneEntryPointRefuses:
    async def test_a_private_database_source_without_a_tunnel(self, database_manager: Any) -> None:
        with patch.object(database_manager, "_execute_external_clone", new_callable=AsyncMock) as execute:
            result = await database_manager.clone_database_from_external_source(
                project_name="demo",
                deployment_name="staging",
                source_host="bron.intern",
                source_port=5432,
                source_username="postgres",
                source_password="geheim",
                source_database="appdb",
                source_schema="app",
            )
        assert result["success"] is False
        assert result["errors"] == [REFUSED_CLONE_SOURCE_REASON]
        execute.assert_not_called()

    async def test_a_private_database_source_behind_a_public_tunnel_goes_through(self, database_manager: Any) -> None:
        with patch.object(database_manager, "_clone_database_with_chisel_tunnel", new_callable=AsyncMock) as tunnel:
            tunnel.return_value = {"success": True}
            result = await database_manager.clone_database_from_external_source(
                project_name="demo",
                deployment_name="staging",
                source_host="bron.intern",
                source_port=5432,
                source_username="postgres",
                source_password="geheim",
                source_database="appdb",
                source_schema="app",
                chisel_config={"server-url": "https://chisel.publiek.nl"},
            )
        assert result["success"] is True
        tunnel.assert_called_once()

    async def test_a_private_chisel_server(self, database_manager: Any) -> None:
        with patch.object(database_manager, "_clone_database_with_chisel_tunnel", new_callable=AsyncMock) as tunnel:
            result = await database_manager.clone_database_from_external_source(
                project_name="demo",
                deployment_name="staging",
                source_host="bron.intern",
                source_port=5432,
                source_username="postgres",
                source_password="geheim",
                source_database="appdb",
                source_schema="app",
                chisel_config={"server-url": "https://chisel.intern"},
            )
        assert result["errors"] == [REFUSED_CLONE_SOURCE_REASON]
        tunnel.assert_not_called()

    async def test_a_private_bucket_source_without_a_tunnel(self, minio_manager: Any) -> None:
        with patch.object(minio_manager, "_execute_external_bucket_clone", new_callable=AsyncMock) as execute:
            result = await minio_manager.clone_bucket_from_external_source(
                project_name="demo",
                deployment_name="staging",
                source_host="bron.intern",
                source_port=9000,
                source_access_key="minioadmin",
                source_secret_key="geheim",
                source_bucket="data",
            )
        assert result["errors"] == [REFUSED_CLONE_SOURCE_REASON]
        execute.assert_not_called()


# ---------------------------------------------------------------------------
# Eén melding naar buiten, het onderscheid in de log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOneMessageForEveryFailedConnection:
    @pytest.mark.parametrize(
        ("error", "logged"),
        [
            ("InvalidCatalogNameError", "does not exist at"),
            ("InvalidPasswordError", "Authentication failed"),
            ("OSError", "Failed to connect"),
        ],
    )
    async def test_the_database_source(
        self,
        database_manager: Any,
        caplog: Any,
        make_asyncpg_error: Callable[[str], Exception],
        error: str,
        logged: str,
    ) -> None:
        with (
            patch("asyncpg.connect", AsyncMock(side_effect=make_asyncpg_error(error))),
            caplog.at_level("WARNING"),
            pytest.raises(Exception, match=UNREACHABLE_SOURCE_REASON),
        ):
            await database_manager._validate_external_source(
                "db.publiek.nl", 5432, "postgres", "geheim", "appdb", "app"
            )
        assert logged in caplog.text
        assert "geheim" not in caplog.text

    async def test_the_bucket_source(self, minio_manager: Any, caplog: Any) -> None:
        connector = MagicMock()
        connector.configure_alias = AsyncMock(return_value=False)
        connector._run_mc_command = AsyncMock()
        with (
            patch("opi.manager.minio_manager.create_minio_connector", return_value=connector),
            caplog.at_level("WARNING"),
            pytest.raises(Exception, match=UNREACHABLE_SOURCE_REASON),
        ):
            await minio_manager._validate_external_source(
                "minio.publiek.nl", 9000, "minioadmin", "geheim", "data", False
            )
        assert "Failed to connect to external MinIO at minio.publiek.nl:9000" in caplog.text


@pytest.fixture
def make_asyncpg_error() -> Callable[[str], Exception]:
    """De asyncpg-fouten die ``_validate_external_source`` apart logt."""
    import asyncpg

    def make(name: str) -> Exception:
        if name == "OSError":
            return OSError("connection refused")
        return getattr(asyncpg, name)("boem")

    return make


# ---------------------------------------------------------------------------
# De wrappers van de API: de tunnel komt van de gedeelde kloonfunctie
# ---------------------------------------------------------------------------


def _wrapper_self(database_manager: Any, minio_manager: Any) -> Any:
    """Een stand-in self voor de ongebonden wrappers van ProjectManager."""
    return SimpleNamespace(
        _ensure_database_manager=AsyncMock(return_value=database_manager),
        _minio_manager=minio_manager,
    )


@pytest.fixture
def chisel_connector() -> Iterator[MagicMock]:
    connector = MagicMock()
    connector.get_local_endpoint.return_value = {"host": "localhost", "port": 15432}
    with patch("opi.utils.chisel_helper.ChiselConnector", return_value=connector):
        yield connector


@pytest.mark.asyncio
class TestTheApiTunnelWrapper:
    async def test_a_private_database_source_behind_a_public_tunnel_is_cloned(
        self, database_manager: Any, minio_manager: Any, chisel_connector: MagicMock
    ) -> None:
        from opi.manager.project_manager import ProjectManager

        with patch.object(database_manager, "_execute_external_clone", new_callable=AsyncMock) as execute:
            execute.return_value = {"success": True, "operations": []}
            result = await ProjectManager.clone_database_from_external_with_tunnel(
                _wrapper_self(database_manager, minio_manager),
                project_name="demo",
                deployment_name="staging",
                source_database="appdb",
                source_schema="app",
                source_username="postgres",
                source_password="geheim",
                tunnel_server_url="https://chisel.publiek.nl",
                tunnel_username="tunnel",
                tunnel_password="geheime-tunnel",
                tunnel_remote_host="bron.intern",
                tunnel_remote_port=5432,
            )

        assert result["success"] is True
        assert result["tunnel"] == {
            "used": True,
            "server": "https://chisel.publiek.nl",
            "local_endpoint": "localhost:15432",
            "remote_endpoint": "bron.intern:5432",
        }
        assert execute.call_args[1]["source_host"] == "localhost"

    async def test_a_private_chisel_server_is_refused_before_the_tunnel(
        self, database_manager: Any, minio_manager: Any, chisel_connector: MagicMock
    ) -> None:
        from opi.manager.project_manager import ProjectManager

        with patch.object(database_manager, "_execute_external_clone", new_callable=AsyncMock) as execute:
            result = await ProjectManager.clone_database_from_external_with_tunnel(
                _wrapper_self(database_manager, minio_manager),
                project_name="demo",
                deployment_name="staging",
                source_database="appdb",
                source_schema="app",
                source_username="postgres",
                source_password="geheim",
                tunnel_server_url="https://chisel.intern",
                tunnel_username="tunnel",
                tunnel_password="geheime-tunnel",
                tunnel_remote_host="bron.intern",
            )

        assert result["errors"] == [REFUSED_CLONE_SOURCE_REASON]
        execute.assert_not_called()
        chisel_connector.start_tunnel.assert_not_called()

    async def test_a_private_bucket_source_behind_a_public_tunnel_is_cloned(
        self, database_manager: Any, minio_manager: Any, chisel_connector: MagicMock
    ) -> None:
        from opi.manager.project_manager import ProjectManager

        with patch.object(minio_manager, "_execute_external_bucket_clone", new_callable=AsyncMock) as execute:
            execute.return_value = {"success": True, "operations": []}
            result = await ProjectManager.clone_minio_bucket_from_external_with_tunnel(
                _wrapper_self(database_manager, minio_manager),
                project_name="demo",
                deployment_name="staging",
                source_bucket="data",
                source_access_key="minioadmin",
                source_secret_key="geheime-sleutel",
                tunnel_server_url="https://chisel.publiek.nl",
                tunnel_username="tunnel",
                tunnel_password="geheime-tunnel",
                tunnel_remote_host="bron.intern",
                tunnel_remote_port=9000,
            )

        assert result["success"] is True
        assert result["tunnel"]["server"] == "https://chisel.publiek.nl"
        assert execute.call_args[1]["source_host"] == "localhost"
