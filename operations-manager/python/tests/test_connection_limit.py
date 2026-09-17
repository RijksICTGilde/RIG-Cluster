"""De connectielimiet van de databaserollen is instelbaar (RC-201).

Gemeten op de drie plekken waar hij iets doet: de samenvoeging (deployment boven project
boven de dienststandaard), de connector (alleen een ALTER ROLE bij een verschil) en de
manager (beide rollen van een deployment, ook als de inloggegevens al kloppen).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.postgres import PostgresConnector, PostgresValidationError
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.visualizers.bridge import editable_to_form_field
from opi.forms.visualizers.providers import ConnectionLimitOptionsProvider
from opi.manager.database_manager import DatabaseManager
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.config_settings import SettingError
from opi.services.catalog.postgresql_database.actions import postgresql_database_actions
from opi.services.catalog.postgresql_database.connection_limit import (
    CONNECTION_LIMIT,
    deployment_connection_limit,
)
from opi.services.catalog.postgresql_database.visualizers import (
    CONNECTION_LIMIT_FIELD,
    DEPLOYMENT_CONNECTION_LIMIT_FIELD,
)
from opi.services.registry import get_service
from opi.services.services import service_entry_name
from opi.services.services_enums import ServiceType

from forms.test_modal_noop_roundtrip import _project as _sweep_project  # type: ignore[import-not-found]
from forms.test_modal_noop_roundtrip import _roundtrip  # type: ignore[import-not-found]

_PG = ServiceType.POSTGRESQL_DATABASE.value


def _project(project_config: dict[str, Any] | None = None, **deployment_configs: dict[str, Any]) -> dict[str, Any]:
    pg: Any = {"name": _PG, "config": project_config} if project_config is not None else _PG
    return {
        "name": "proj",
        "services": [pg],
        "deployments": [
            {
                "name": name,
                "cluster": "local",
                "services": [{"reference": _PG, "config": config}] if config is not None else [_PG],
            }
            for name, config in (deployment_configs or {"test": None}).items()
        ],
    }


# --- samenvoegen ------------------------------------------------------------------


def test_zonder_waarde_blijft_het_twintig() -> None:
    assert deployment_connection_limit(_project(), "test") == 20
    assert deployment_connection_limit(_project({}, test={}), "test") == 20


def test_de_projectwaarde_geldt_voor_elke_deployment() -> None:
    project = _project({"connection-limit": 30}, test=None, productie={})
    assert deployment_connection_limit(project, "test") == 30
    assert deployment_connection_limit(project, "productie") == 30


def test_een_deployment_overschrijft_alleen_zichzelf() -> None:
    project = _project({"connection-limit": 30}, test=None, pr_250={"connection-limit": 80})
    assert deployment_connection_limit(project, "pr_250") == 80
    assert deployment_connection_limit(project, "test") == 30


# --- de speelruimte ---------------------------------------------------------------


@pytest.mark.parametrize("waarde", [0, 501, True])
@pytest.mark.parametrize("laag", ["project", "deployment"])
def test_buiten_de_speelruimte_wordt_geweigerd_met_de_melding_van_check(waarde: object, laag: str) -> None:
    config = {"connection-limit": waarde}
    project = _project(config) if laag == "project" else _project({}, test=config)
    with pytest.raises(SettingError) as verwacht:
        CONNECTION_LIMIT.check(waarde)
    with pytest.raises(ProjectIntegrityError) as geweigerd:
        validate_service_configs(project)
    assert str(verwacht.value) in str(geweigerd.value)


def test_elke_waarde_binnen_de_speelruimte_is_geldig() -> None:
    validate_service_configs(_project({"connection-limit": 500}, test={"connection-limit": 37}))
    validate_service_configs(_project({"scope": "project", "connection-limit": 1}))


def test_de_grenzen_staan_alleen_in_de_declaratie() -> None:
    assert (CONNECTION_LIMIT.minimum, CONNECTION_LIMIT.maximum, CONNECTION_LIMIT.default) == (1, 500, 20)
    assert get_service(ServiceType.POSTGRESQL_DATABASE).config_settings() == (CONNECTION_LIMIT,)


# --- de connector -----------------------------------------------------------------


def _connector(rolconnlimit: int | None) -> tuple[PostgresConnector, AsyncMock]:
    connector = PostgresConnector("h", "admin", "pw")
    conn = AsyncMock()
    conn.fetchval.return_value = rolconnlimit
    connector._get_or_create_connection = AsyncMock(return_value=conn)  # type: ignore[method-assign]
    return connector, conn


@pytest.mark.asyncio
async def test_een_nieuwe_rol_krijgt_de_meegegeven_limiet() -> None:
    connector, conn = _connector(None)
    await connector.create_user("proj_test", "Wachtwoord123abc", connection_limit=37)
    (sql,) = [call.args[0] for call in conn.execute.await_args_list]
    assert sql.endswith("CONNECTION LIMIT 37")


@pytest.mark.asyncio
async def test_een_bestaande_rol_volgt_een_gewijzigde_waarde() -> None:
    connector, conn = _connector(20)
    result = await connector.set_connection_limit("proj_test", 80)
    assert result == {"status": "updated", "previous": 20, "connection_limit": 80}
    conn.fetchval.assert_awaited_once_with("SELECT rolconnlimit FROM pg_roles WHERE rolname = $1", "proj_test")
    conn.execute.assert_awaited_once_with('ALTER ROLE "proj_test" CONNECTION LIMIT 80')


@pytest.mark.asyncio
async def test_zonder_wijziging_geen_alter_role() -> None:
    connector, conn = _connector(20)
    result = await connector.set_connection_limit("proj_test", 20)
    assert result["status"] == "unchanged"
    conn.execute.assert_not_awaited()


@pytest.mark.parametrize("waarde", [0, -1, True, "20"])
@pytest.mark.asyncio
async def test_de_connector_zet_alleen_een_positief_getal_in_ddl(waarde: Any) -> None:
    connector, conn = _connector(20)
    with pytest.raises(PostgresValidationError):
        await connector.set_connection_limit("proj_test", waarde)
    with pytest.raises(PostgresValidationError):
        await connector.create_user("proj_test", "Wachtwoord123abc", connection_limit=waarde)
    conn.execute.assert_not_awaited()


# --- de manager -------------------------------------------------------------------


class _Rollen:
    """Een server met rollen en hun limiet, achter de connectoraanroepen die de manager doet."""

    def __init__(self, **limieten: int) -> None:
        self.limieten = dict(limieten)
        self.alters: list[tuple[str, int]] = []
        self.wachtwoorden: list[str] = []

    async def create_user(
        self, username: str, password: str, database_privileges: Any = None, *, connection_limit: int
    ) -> dict[str, str]:
        if username in self.limieten:
            return {"status": "exists"}
        self.limieten[username] = connection_limit
        return {"status": "created"}

    async def set_connection_limit(self, username: str, connection_limit: int) -> dict[str, Any]:
        vorige = self.limieten[username]
        if vorige == connection_limit:
            return {"status": "unchanged", "previous": vorige, "connection_limit": connection_limit}
        self.limieten[username] = connection_limit
        self.alters.append((username, connection_limit))
        return {"status": "updated", "previous": vorige, "connection_limit": connection_limit}

    async def update_user_password(self, username: str, new_password: str) -> dict[str, str]:
        self.wachtwoorden.append(username)
        return {"status": "success"}

    async def noop(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        return {"status": "success"}


def _manager(rollen: _Rollen, stappen: list[tuple[str, str | None]]) -> DatabaseManager:
    progress = SimpleNamespace(
        add_task=lambda name, subject=None: stappen.append((name, subject)) or str(len(stappen)),
        complete_task=lambda task_id: None,
    )
    project_manager = SimpleNamespace(
        get_name=AsyncMock(return_value="proj"),
        get_progress_manager=lambda: progress,
        _add_secret_to_create=lambda *args: None,
    )
    mgr = DatabaseManager(project_manager=project_manager, db_host="h", admin_username="a", admin_password="p")
    connector = SimpleNamespace(
        create_user=rollen.create_user,
        set_connection_limit=rollen.set_connection_limit,
        update_user_password=rollen.update_user_password,
        update_user_privileges=rollen.noop,
        create_schema=rollen.noop,
        set_role_search_path=rollen.noop,
        grant_readonly_on_schema=rollen.noop,
    )
    mgr._postgres_connector = connector  # type: ignore[assignment]
    return mgr


async def _verwerk(mgr: DatabaseManager, project: dict[str, Any], deployment: str) -> None:
    """Een herverwerking van een deployment waarvan de inloggegevens al kloppen."""
    bestaand = SimpleNamespace(password="Wachtwoord123abc", ro_password="Wachtwoord123ro")
    state = SimpleNamespace(database="proj_test", schema="proj_test", password="Wachtwoord123abc")
    deployment_dict = next(d for d in project["deployments"] if d["name"] == deployment)
    with (
        patch.object(DatabaseManager, "_deployment_uses_postgresql", AsyncMock(return_value=True)),
        patch.object(DatabaseManager, "_ensure_connection", lambda self: None),
        patch.object(DatabaseManager, "_get_existing_database_credentials_from_k8s", AsyncMock(return_value=bestaand)),
        patch.object(DatabaseManager, "_test_database_connection", AsyncMock(return_value=True)),
        patch.object(DatabaseManager, "_ensure_database_state", AsyncMock(return_value=state)),
        patch.object(DatabaseManager, "_get_deployment_database_generation", lambda self, *args: None),
        patch("opi.manager.database_manager.get_database_server", return_value="h"),
    ):
        await mgr.create_resources_for_deployment(project, deployment_dict, False)


@pytest.mark.asyncio
async def test_herverwerken_zet_beide_rollen_op_de_nieuwe_waarde() -> None:
    rollen = _Rollen(proj_test=20, proj_test_ro=20)
    stappen: list[tuple[str, str | None]] = []
    await _verwerk(_manager(rollen, stappen), _project({}, test={"connection-limit": 80}), "test")

    assert rollen.limieten == {"proj_test": 80, "proj_test_ro": 80}
    assert ("Connectielimiet", "proj_test: van 20 naar 80") in stappen
    assert ("Connectielimiet", "proj_test_ro: van 20 naar 80") in stappen
    # De limiet lift niet mee op de wachtwoordtak: de rw-rol houdt zijn wachtwoord.
    assert "proj_test" not in rollen.wachtwoorden


@pytest.mark.asyncio
async def test_herverwerken_zonder_wijziging_stuurt_geen_alter() -> None:
    rollen = _Rollen(proj_test=30, proj_test_ro=30)
    stappen: list[tuple[str, str | None]] = []
    await _verwerk(_manager(rollen, stappen), _project({"connection-limit": 30}), "test")

    assert rollen.alters == []
    assert ("Connectielimiet", "proj_test: 30, ongewijzigd") in stappen


@pytest.mark.asyncio
async def test_een_nieuwe_deployment_maakt_beide_rollen_met_de_limiet() -> None:
    rollen = _Rollen()
    mgr = _manager(rollen, [])
    with patch.object(DatabaseManager, "_get_existing_database_credentials_from_k8s", AsyncMock(return_value=None)):
        await mgr._resolve_database_credentials(
            "proj",
            "test",
            {"name": "test"},
            "proj_test",
            "proj_test",
            "proj_test",
            db_host="h",
            admin_username="a",
            admin_password="p",
            connection_limit=40,
        )
        await mgr._ensure_readonly_user("test", {"name": "test"}, "proj_test", "proj_test", ["proj_test"], 40)

    assert rollen.limieten == {"proj_test": 40, "proj_test_ro": 40}
    assert rollen.alters == []


# --- de wizard --------------------------------------------------------------------


def test_de_keuzelijst_toont_de_tien_stappen() -> None:
    opties = ConnectionLimitOptionsProvider().get_options()
    assert [o["value"] for o in opties] == ["", "10", "20", "40", "50", "75", "100", "150", "200", "250", "500"]
    assert opties[0]["label"] == "Standaard van het platform (20)"


def test_een_eigen_waarde_staat_op_zijn_plek_in_de_lijst() -> None:
    project = _project({"connection-limit": 37})
    veld = editable_to_form_field(CONNECTION_LIMIT_FIELD, project, edit_mode=True)
    assert veld.value == "37"
    waarden = [o["value"] for o in veld.options]
    assert waarden.index("37") == waarden.index("20") + 1
    assert {"value": "37", "label": "37 (eigen waarde)"} in veld.options


def test_de_deployment_toont_wat_er_geldt_zonder_eigen_waarde() -> None:
    project = _project({"connection-limit": 30})
    veld = editable_to_form_field(
        get_service(ServiceType.POSTGRESQL_DATABASE).deployment_form_section(0).editables[0], project, edit_mode=True
    )
    assert veld.value == ""
    assert veld.options[0] == {"value": "", "label": "Volg het project (30)"}


def test_een_leeg_veld_schrijft_geen_standaard_weg() -> None:
    assert CONNECTION_LIMIT_FIELD.editable.default is None
    assert DEPLOYMENT_CONNECTION_LIMIT_FIELD.editable.default is None
    assert DEPLOYMENT_CONNECTION_LIMIT_FIELD.editable.remove_when_none


def _met_limieten(project: dict[str, Any], deployment_waarde: int | None) -> dict[str, Any]:
    project = copy.deepcopy(project)
    for entry in project["services"]:
        if service_entry_name(entry) == _PG:
            entry["config"]["connection-limit"] = 37
    if deployment_waarde is not None:
        project["deployments"][0]["services"].append(
            {"reference": _PG, "config": {"connection-limit": deployment_waarde}}
        )
    return project


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_id", ["modal-edit-postgresql-schemas", "modal-edit-postgresql-deployment-0"])
async def test_een_waarde_van_37_overleeft_de_wizard(flow_id: str) -> None:
    with patch("forms.test_modal_noop_roundtrip._project", side_effect=lambda: _met_limieten(_sweep_project(), 37)):
        _expected, result = await _roundtrip(flow_id)
    project_entry = next(e for e in result["services"] if service_entry_name(e) == _PG)
    assert project_entry["config"]["connection-limit"] == 37
    deployment_entry = next(e for e in result["deployments"][0]["services"] if service_entry_name(e) == _PG)
    assert deployment_entry["config"]["connection-limit"] == 37


@pytest.mark.asyncio
async def test_een_deployment_zonder_waarde_krijgt_er_geen_bij() -> None:
    with patch("forms.test_modal_noop_roundtrip._project", side_effect=lambda: _met_limieten(_sweep_project(), None)):
        _expected, result = await _roundtrip("modal-edit-postgresql-deployment-0")
    for entry in result["deployments"][0].get("services", []):
        if service_entry_name(entry) == _PG:
            assert "connection-limit" not in (entry.get("config") or {})


# --- de knop ----------------------------------------------------------------------


def test_de_deploymentkaart_krijgt_een_knop_naar_de_limiet() -> None:
    project = _project({}, test=None, productie=None)
    (knop,) = [a for a in postgresql_database_actions(project, "productie") if a.label == "Connectielimiet"]
    assert knop.modal_endpoint == "/projects/proj/modal-wizard/modal-edit-postgresql-deployment-1"
