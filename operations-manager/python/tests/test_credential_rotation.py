"""Een rotatie laat geen werkende oude waarde achter (RC-300).

De acceptatie op de sandbox vond twee waarden die bleven werken terwijl de aanvraag succes
meldde: het Keycloak-clientsecret in de tak die de client aanmaakt, en het wachtwoord van de
read-only databaserol. Beide takken staan hier vast, naast de gewone rotatie per dienst.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from keycloak.exceptions import KeycloakError
from opi.connectors.keycloak import KeycloakConnector
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.database_manager import DatabaseManager
from opi.manager.keycloak_manager import KeycloakManager
from opi.manager.minio_manager import MinioManager
from opi.services.credential_rotation import rotatable_services_for_deployment, services_with_pending_clone


class _Rollen:
    """Een postgres-connector die alleen bijhoudt wat er met de rollen gebeurt."""

    def __init__(self, *bestaand: str) -> None:
        self.bestaand = set(bestaand)
        self.gezet: dict[str, str] = {}

    async def create_user(
        self, username: str, password: str, database_privileges: Any = None, *, connection_limit: int
    ) -> dict[str, str]:
        if username in self.bestaand:
            return {"status": "exists"}
        self.bestaand.add(username)
        self.gezet[username] = password
        return {"status": "created"}

    async def update_user_password(self, username: str, new_password: str) -> dict[str, str]:
        self.gezet[username] = new_password
        return {"status": "success"}

    async def set_connection_limit(self, username: str, connection_limit: int) -> dict[str, Any]:
        return {"status": "unchanged", "previous": connection_limit, "connection_limit": connection_limit}

    async def noop(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        return {"status": "success"}


def _db_manager(rollen: _Rollen) -> DatabaseManager:
    project_manager = SimpleNamespace(
        get_name=AsyncMock(return_value="proj"),
        get_progress_manager=lambda: None,
        _add_secret_to_create=lambda *args: None,
    )
    mgr = DatabaseManager(project_manager=project_manager, db_host="h", admin_username="a", admin_password="p")
    mgr._postgres_connector = SimpleNamespace(  # type: ignore[assignment]
        create_user=rollen.create_user,
        update_user_password=rollen.update_user_password,
        set_connection_limit=rollen.set_connection_limit,
        update_user_privileges=rollen.noop,
        create_schema=rollen.noop,
        set_role_search_path=rollen.noop,
        grant_readonly_on_schema=rollen.noop,
    )
    return mgr


# --- PostgreSQL -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_de_vlag_zet_een_nieuw_wachtwoord_zonder_het_oude_te_lezen() -> None:
    """Met de vlag gaat de hergebruiktak over: het opgeslagen Secret wordt niet eens gelezen."""
    rollen = _Rollen("proj_test")
    mgr = _db_manager(rollen)
    lezen = AsyncMock(return_value=SimpleNamespace(password="dummy-oud-wachtwoord", ro_password="dummy-oud-readonly"))

    with patch.object(DatabaseManager, "_get_existing_database_credentials_from_k8s", lezen):
        nieuw = await mgr._resolve_database_credentials(
            "proj",
            "test",
            {"name": "test"},
            "proj_test",
            "proj_test",
            "proj_test",
            db_host="h",
            admin_username="a",
            admin_password="p",
            connection_limit=20,
            rotate_password=True,
        )

    assert nieuw != "dummy-oud-wachtwoord"
    assert rollen.gezet["proj_test"] == nieuw
    lezen.assert_not_awaited()


@pytest.mark.asyncio
async def test_zonder_de_vlag_blijft_het_wachtwoord_hergebruikt() -> None:
    rollen = _Rollen("proj_test")
    mgr = _db_manager(rollen)

    with (
        patch.object(
            DatabaseManager,
            "_get_existing_database_credentials_from_k8s",
            AsyncMock(return_value=SimpleNamespace(password="dummy-oud-wachtwoord", ro_password="dummy-oud-readonly")),
        ),
        patch.object(DatabaseManager, "_test_database_connection", AsyncMock(return_value=True)),
    ):
        zelfde = await mgr._resolve_database_credentials(
            "proj",
            "test",
            {"name": "test"},
            "proj_test",
            "proj_test",
            "proj_test",
            db_host="h",
            admin_username="a",
            admin_password="p",
            connection_limit=20,
        )

    assert zelfde == "dummy-oud-wachtwoord"
    assert rollen.gezet == {}


@pytest.mark.asyncio
async def test_de_read_only_rol_rouleert_mee() -> None:
    """Die rol leest elk schema, dus een rotatie die hem laat staan laat een oude waarde staan."""
    rollen = _Rollen("proj_test", "proj_test_ro")
    mgr = _db_manager(rollen)
    lezen = AsyncMock(return_value=SimpleNamespace(password="dummy-oud-wachtwoord", ro_password="dummy-oud-readonly"))

    with patch.object(DatabaseManager, "_get_existing_database_credentials_from_k8s", lezen):
        _, ro_wachtwoord = await mgr._ensure_readonly_user(
            "test", {"name": "test"}, "proj_test", "proj_test", ["proj_test"], 20, db_host="h", rotate_password=True
        )

    assert ro_wachtwoord != "dummy-oud-readonly"
    assert rollen.gezet["proj_test_ro"] == ro_wachtwoord
    lezen.assert_not_awaited()


@pytest.mark.asyncio
async def test_zonder_de_vlag_houdt_de_read_only_rol_zijn_wachtwoord() -> None:
    rollen = _Rollen("proj_test", "proj_test_ro")
    mgr = _db_manager(rollen)

    with (
        patch.object(
            DatabaseManager,
            "_get_existing_database_credentials_from_k8s",
            AsyncMock(return_value=SimpleNamespace(password="dummy-oud-wachtwoord", ro_password="dummy-oud-readonly")),
        ),
        patch.object(DatabaseManager, "_test_database_connection", AsyncMock(return_value=True)),
    ):
        _, ro_wachtwoord = await mgr._ensure_readonly_user(
            "test", {"name": "test"}, "proj_test", "proj_test", ["proj_test"], 20, db_host="h"
        )

    assert ro_wachtwoord == "dummy-oud-readonly"
    assert rollen.gezet == {}


@pytest.mark.asyncio
async def test_read_only_password_is_set_again_when_the_stored_one_is_rejected() -> None:
    rollen = _Rollen("proj_test", "proj_test_ro")
    mgr = _db_manager(rollen)

    with (
        patch.object(
            DatabaseManager,
            "_get_existing_database_credentials_from_k8s",
            AsyncMock(return_value=SimpleNamespace(password="dummy-oud-wachtwoord", ro_password="dummy-oud-readonly")),
        ),
        patch.object(DatabaseManager, "_test_database_connection", AsyncMock(return_value=False)),
    ):
        _, ro_wachtwoord = await mgr._ensure_readonly_user(
            "test", {"name": "test"}, "proj_test", "proj_test", ["proj_test"], 20, db_host="h"
        )

    assert ro_wachtwoord == "dummy-oud-readonly"
    assert rollen.gezet == {"proj_test_ro": "dummy-oud-readonly"}


@pytest.mark.asyncio
async def test_connect_denied_after_authentication_counts_as_valid_credentials() -> None:
    """PostgreSQL checks the password before CONNECT, so 42501 means the password was accepted."""
    weigering = asyncpg.InsufficientPrivilegeError('permission denied for database "proj_test"')

    with patch("opi.manager.database_manager.asyncpg.connect", AsyncMock(side_effect=weigering)):
        geldig = await DatabaseManager._test_database_connection("proj_test_ro", "pw", "proj_test", "proj_test", "h")

    assert geldig is True


# --- Keycloak ---------------------------------------------------------------------


def _connector(secret_data: Any) -> tuple[KeycloakConnector, MagicMock]:
    conn = KeycloakConnector.__new__(KeycloakConnector)
    admin = MagicMock()
    admin.generate_client_secrets.return_value = secret_data
    conn.admin = admin  # type: ignore[attr-defined]
    return conn, admin


@pytest.mark.asyncio
async def test_de_connector_laat_keycloak_een_nieuw_secret_maken() -> None:
    conn, admin = _connector({"value": "nieuw-secret"})

    assert await conn.regenerate_client_secret("intern-id", "proj-realm") == "nieuw-secret"
    admin.generate_client_secrets.assert_called_once_with(client_id="intern-id")
    # Het realm wordt gezet en daarna weer op master teruggezet.
    assert [c.args[0] for c in admin.change_current_realm.call_args_list] == ["proj-realm", "master"]


@pytest.mark.asyncio
async def test_een_antwoord_zonder_waarde_is_een_fout() -> None:
    """Stil een leeg secret opslaan zou de deployment onbruikbaar maken."""
    conn, _ = _connector({})

    with pytest.raises(RuntimeError, match="no secret"):
        await conn.regenerate_client_secret("intern-id", "proj-realm")


@pytest.mark.asyncio
async def test_een_fout_van_keycloak_zet_het_realm_terug() -> None:
    conn, admin = _connector(None)
    admin.generate_client_secrets.side_effect = KeycloakError("kapot")

    with pytest.raises(RuntimeError, match="Failed to regenerate client secret"):
        await conn.regenerate_client_secret("intern-id", "proj-realm")
    assert admin.change_current_realm.call_args_list[-1].args[0] == "master"


@pytest.mark.asyncio
async def test_de_manager_zoekt_de_client_en_vernieuwt_zijn_secret() -> None:
    mgr = KeycloakManager.__new__(KeycloakManager)
    keycloak = SimpleNamespace(
        find_client_by_client_id=AsyncMock(return_value={"id": "intern-id"}),
        regenerate_client_secret=AsyncMock(return_value="nieuw-secret"),
    )

    with patch("opi.manager.keycloak_manager.create_keycloak_connector", AsyncMock(return_value=keycloak)):
        nieuw = await mgr._regenerate_deployment_client_secret("https://kc", "proj-realm", "proj-productie")

    assert nieuw == "nieuw-secret"
    keycloak.regenerate_client_secret.assert_awaited_once_with("intern-id", "proj-realm")


@pytest.mark.asyncio
async def test_een_client_die_niet_bestaat_breekt_de_rotatie_af() -> None:
    mgr = KeycloakManager.__new__(KeycloakManager)
    keycloak = SimpleNamespace(find_client_by_client_id=AsyncMock(return_value=None))

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(ValueError, match="not found in realm"),
    ):
        await mgr._regenerate_deployment_client_secret("https://kc", "proj-realm", "proj-productie")


def _sso_manager(bestaand_secret: Any) -> KeycloakManager:
    """Een KeycloakManager waarvan alleen de clienttak van de SSO-opbouw echt loopt."""
    mgr = KeycloakManager.__new__(KeycloakManager)
    mgr.project_manager = SimpleNamespace(  # type: ignore[attr-defined]
        _get_project_keycloak_config_for_cluster=AsyncMock(return_value={"realm": "proj-realm", "host": "https://kc"}),
        _get_keycloak_url_for_cluster=lambda cluster: "https://kc",
        _get_secret_from_map=lambda *args: bestaand_secret,
        _add_secret_to_create=lambda *args: None,
    )
    return mgr


_SSO_PRELUDE = (
    "_project_realm_is_present",
    "_ensure_realm_authentication_flow",
    "_ensure_idp_and_platform_client_configuration",
    "_ensure_realm_identity_providers",
    "_ensure_realm_self_service",
    "_ensure_realm_clients",
    "_ensure_admin_otp",
)


def _patch_sso(stack: ExitStack, keycloak: Any, *, support_http: bool) -> None:
    """Zet de omliggende realm-opbouw stil zodat alleen de clienttak echt loopt."""
    stack.enter_context(
        patch("opi.manager.keycloak_manager.create_keycloak_connector", AsyncMock(return_value=keycloak))
    )
    stack.enter_context(patch("opi.manager.keycloak_manager.get_keycloak_discovery_url", lambda cluster: "https://kc"))
    if support_http:
        stack.enter_context(patch("opi.manager.keycloak_manager.get_keycloak_support_http", lambda cluster: False))
    for naam in _SSO_PRELUDE:
        stack.enter_context(patch.object(KeycloakManager, naam, AsyncMock(return_value=True)))


@pytest.mark.asyncio
async def test_de_aanmaaktak_vernieuwt_het_secret_van_een_bestaande_client() -> None:
    """create_deployment_client is idempotent en geeft het HUIDIGE secret terug.

    Zonder de vernieuwslag meldt een rotatie dus succes terwijl het oude clientsecret blijft
    werken. Dat is precies wat de meting op de sandbox aantrof.
    """
    mgr = _sso_manager(None)
    keycloak = SimpleNamespace(
        create_deployment_client=AsyncMock(
            return_value={"client_id": "proj-productie", "client_secret": "oud-secret", "public_client_id": "pub"}
        ),
        find_client_by_client_id=AsyncMock(return_value={"id": "intern-id"}),
        regenerate_client_secret=AsyncMock(return_value="nieuw-secret"),
    )
    geschreven: dict[str, Any] = {}
    mgr.project_manager._add_secret_to_create = (  # type: ignore[attr-defined]
        lambda _d, _n, secret: geschreven.update(secret=secret)
    )

    with ExitStack() as stack:
        _patch_sso(stack, keycloak, support_http=True)
        resultaat = await mgr._setup_sso_rijk_integration(
            project_name="proj",
            deployment_name="productie",
            ingress_hosts=["web.example.org"],
            cluster="local",
            config={"template": "sso-only"},
            rotate_client_secret=True,
        )

    assert resultaat is not None
    assert resultaat["client_secret"] == "nieuw-secret"
    assert geschreven["secret"].client_secret == "nieuw-secret"
    keycloak.regenerate_client_secret.assert_awaited_once_with("intern-id", "proj-realm")


@pytest.mark.asyncio
async def test_de_aanmaaktak_laat_het_secret_staan_zonder_rotatie() -> None:
    mgr = _sso_manager(None)
    keycloak = SimpleNamespace(
        create_deployment_client=AsyncMock(
            return_value={"client_id": "proj-productie", "client_secret": "oud-secret", "public_client_id": "pub"}
        ),
        regenerate_client_secret=AsyncMock(),
    )

    with ExitStack() as stack:
        _patch_sso(stack, keycloak, support_http=True)
        resultaat = await mgr._setup_sso_rijk_integration(
            project_name="proj",
            deployment_name="productie",
            ingress_hosts=["web.example.org"],
            cluster="local",
            config={"template": "sso-only"},
        )

    assert resultaat is not None
    assert resultaat["client_secret"] == "oud-secret"
    keycloak.regenerate_client_secret.assert_not_awaited()


@pytest.mark.asyncio
async def test_de_hergebruiktak_vernieuwt_het_secret_ook() -> None:
    mgr = _sso_manager(
        SimpleNamespace(
            client_id="proj-productie",
            client_secret="oud-secret",
            public_client_id="pub",
            discovery_url="https://kc/realms/proj-realm/.well-known/openid-configuration",
            base_url="https://kc",
            realm="proj-realm",
        )
    )
    keycloak = SimpleNamespace(
        find_client_by_client_id=AsyncMock(return_value={"id": "intern-id"}),
        regenerate_client_secret=AsyncMock(return_value="nieuw-secret"),
    )

    with ExitStack() as stack:
        _patch_sso(stack, keycloak, support_http=False)
        resultaat = await mgr._setup_sso_rijk_integration(
            project_name="proj",
            deployment_name="productie",
            ingress_hosts=["web.example.org"],
            cluster="local",
            config={"template": "sso-only"},
            rotate_client_secret=True,
        )

    assert resultaat is not None
    assert resultaat["client_secret"] == "nieuw-secret"


# --- welke diensten kunnen ---------------------------------------------------------


def _project(*diensten: str) -> dict[str, Any]:
    return {
        "name": "proj",
        "components": [{"name": "web", "services": list(diensten)}],
        "deployments": [{"name": "productie", "components": [{"reference": "web"}]}],
    }


def test_alleen_de_diensten_die_de_deployment_gebruikt() -> None:
    assert rotatable_services_for_deployment(_project("postgresql-database", "publish-on-web"), "productie") == [
        "postgresql-database"
    ]


def test_een_database_per_namespace_levert_hetzelfde_geheim() -> None:
    assert rotatable_services_for_deployment(_project("namespace-postgresql-database"), "productie") == [
        "postgresql-database"
    ]


def test_alle_drie_als_de_deployment_ze_alle_drie_heeft() -> None:
    project = _project("postgresql-database", "minio-storage", "keycloak")
    assert set(rotatable_services_for_deployment(project, "productie")) == {
        "postgresql-database",
        "minio-storage",
        "keycloak",
    }


def test_een_deployment_zonder_zulke_diensten_levert_niets() -> None:
    assert rotatable_services_for_deployment(_project("publish-on-web"), "productie") == []


# --- MinIO ------------------------------------------------------------------------


class _Gebruikers:
    """Een MinIO-connector die alleen bijhoudt welke sleutel de gebruiker krijgt."""

    def __init__(self) -> None:
        self.gezet: dict[str, str] = {}
        self.verwijderd: list[str] = []

    async def configure_alias(self, **kwargs: Any) -> bool:
        return True

    async def delete_user(self, alias: str, username: str) -> dict[str, str]:
        self.verwijderd.append(username)
        return {"status": "deleted"}

    async def create_user(self, alias: str, username: str, secret_key: str) -> dict[str, str]:
        self.gezet[username] = secret_key
        return {"status": "created"}

    async def create_bucket(self, alias: str, bucket: str) -> dict[str, str]:
        return {"status": "exists"}

    async def grant_bucket_access(self, alias: str, username: str, bucket: str, rechten: list[str]) -> dict[str, str]:
        return {"status": "granted"}


def _minio_manager() -> tuple[MinioManager, dict[str, Any]]:
    geschreven: dict[str, Any] = {}
    project_manager = SimpleNamespace(
        get_name=AsyncMock(return_value="proj"),
        get_progress_manager=lambda: None,
        _add_secret_to_create=lambda _d, _n, secret: geschreven.update(secret=secret),
        _project_file_handler=ProjectFileHandler(),
    )
    return MinioManager(project_manager=project_manager), geschreven


@contextmanager
def _patch_minio(gebruikers: _Gebruikers, *, verbinding_werkt: bool) -> Any:
    """Zet de omgeving van de MinIO-tak stil: alleen het credentialblok loopt echt."""
    bestaand = SimpleNamespace(access_key="proj_test", secret_key="dummy-oud-secretkey")
    with ExitStack() as stack:
        stack.enter_context(patch("opi.manager.minio_manager.create_minio_connector", lambda: gebruikers))
        stack.enter_context(patch.object(MinioManager, "_deployment_uses_minio", AsyncMock(return_value=True)))
        stack.enter_context(
            patch.object(MinioManager, "_get_existing_minio_credentials_from_k8s", AsyncMock(return_value=bestaand))
        )
        toets = stack.enter_context(
            patch.object(MinioManager, "_test_minio_connection", AsyncMock(return_value=verbinding_werkt))
        )
        stack.enter_context(patch.object(MinioManager, "_check_minio_user_exists", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(MinioManager, "_apply_bucket_versioning", AsyncMock()))
        stack.enter_context(patch.object(MinioManager, "_apply_noncurrent_version_limit", AsyncMock()))
        stack.enter_context(patch("opi.manager.minio_manager.get_minio_host", lambda cluster: "minio"))
        stack.enter_context(patch("opi.manager.minio_manager.get_minio_port", lambda cluster: 9000))
        yield toets


@pytest.mark.asyncio
async def test_de_vlag_geeft_de_minio_gebruiker_een_nieuwe_secret_key() -> None:
    """Met de vlag wordt de opgeslagen key niet eens getoetst: de gebruiker gaat om."""
    gebruikers = _Gebruikers()
    mgr, geschreven = _minio_manager()

    with _patch_minio(gebruikers, verbinding_werkt=True) as toets:
        await mgr.create_resources_for_deployment(
            {"name": "proj"}, {"name": "test", "cluster": "local"}, rotate_secret_key=True
        )

    nieuw = gebruikers.gezet["proj_test"]
    assert nieuw != "dummy-oud-secretkey"
    assert gebruikers.verwijderd == ["proj_test"]
    assert geschreven["secret"].secret_key == nieuw
    toets.assert_not_awaited()


@pytest.mark.asyncio
async def test_zonder_de_vlag_blijft_de_minio_secret_key_hergebruikt() -> None:
    gebruikers = _Gebruikers()
    mgr, geschreven = _minio_manager()

    with _patch_minio(gebruikers, verbinding_werkt=True):
        await mgr.create_resources_for_deployment({"name": "proj"}, {"name": "test", "cluster": "local"})

    assert gebruikers.gezet == {}
    assert geschreven["secret"].secret_key == "dummy-oud-secretkey"


@pytest.mark.asyncio
async def test_een_kapotte_key_blijft_de_instelling_volgen() -> None:
    """De herstel-tak hangt nog aan RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE, niet aan de vlag."""
    gebruikers = _Gebruikers()
    mgr, geschreven = _minio_manager()

    with (
        _patch_minio(gebruikers, verbinding_werkt=False),
        patch("opi.manager.minio_manager.settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE", False),
    ):
        await mgr.create_resources_for_deployment({"name": "proj"}, {"name": "test", "cluster": "local"})

    assert gebruikers.gezet == {}
    assert geschreven == {}


# --- een lopende clone-from -------------------------------------------------------


def _kloonproject(clone_from: dict[str, Any]) -> dict[str, Any]:
    project = _project("postgresql-database", "minio-storage", "keycloak")
    project["deployments"][0]["clone-from"] = clone_from
    return project


def test_een_lopende_externe_kloon_bepaalt_database_en_bucket_zelf() -> None:
    """De klooproute lost de credentials opnieuw op zonder de vlag, dus rotatie kan daar niet naast."""
    clone_from = {"type": "remote-source", "reference": "bron", "mode": "always"}
    assert set(services_with_pending_clone(_kloonproject(clone_from), "productie")) == {
        "postgresql-database",
        "minio-storage",
    }


def test_een_lopende_deploymentkloon_bepaalt_alleen_de_bucket() -> None:
    """De databasekloon draagt het wachtwoord mee, de bucketkloon hergebruikt een werkende key."""
    clone_from = {"type": "deployment", "reference": "acceptatie", "mode": "once"}
    assert services_with_pending_clone(_kloonproject(clone_from), "productie") == ["minio-storage"]


def test_een_afgeronde_eenmalige_kloon_blokkeert_niets() -> None:
    clone_from = {"type": "remote-source", "reference": "bron", "mode": "once", "status": {"completed": True}}
    assert services_with_pending_clone(_kloonproject(clone_from), "productie") == []


def test_force_clone_maakt_een_afgeronde_kloon_weer_lopend() -> None:
    clone_from = {"type": "remote-source", "reference": "bron", "mode": "once", "status": {"completed": True}}
    project = _kloonproject(clone_from)
    project["deployments"][0]["force-clone"] = True
    assert set(services_with_pending_clone(project, "productie")) == {"postgresql-database", "minio-storage"}


def test_een_backup_kloon_blokkeert_niets() -> None:
    """Type 'backup' valt in beide managers door naar de gewone tak."""
    assert services_with_pending_clone(_kloonproject({"type": "backup", "mode": "once"}), "productie") == []


def test_een_deployment_zonder_clone_from_blokkeert_niets() -> None:
    assert services_with_pending_clone(_project("postgresql-database"), "productie") == []
