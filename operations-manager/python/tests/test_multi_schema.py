"""RC-17 step 2: multiple schemas.

Covers the naming helpers, the variable-name reservation, the DatabaseSecret's extra
schema variables + search_path, and the manager orchestration (create every schema,
grant the read-only role on every schema, search_path over the full list).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from opi.connectors.postgres import PostgresConnector
from opi.manager.database_manager import DatabaseManager
from opi.services.catalog.postgresql_database.variables import reserved_database_variable_names
from opi.utils.naming import generate_extra_database_schema, generate_schema_variable_name
from opi.utils.secrets import DatabaseSecret

# --- naming ---------------------------------------------------------------------


def test_extra_schema_full_name():
    assert generate_extra_database_schema("My-Proj", "Dep-1", "rapportage") == "my_proj_dep_1_rapportage"


def test_extra_schema_too_long_fails_loudly():
    with pytest.raises(ValueError, match="exceeds the 63-character"):
        generate_extra_database_schema("p" * 40, "d" * 30, "rapportage")


def test_schema_variable_name():
    assert generate_schema_variable_name("rapportage") == "DATABASE_SCHEMA_RAPPORTAGE"
    assert generate_schema_variable_name("audit_2024") == "DATABASE_SCHEMA_AUDIT_2024"


def test_reserved_names_include_the_nine_variables():
    reserved = reserved_database_variable_names()
    assert {"DATABASE_DB", "DATABASE_SCHEMA", "DATABASE_SERVER_HOST", "APP_DATABASE_DB"} <= reserved
    # A postfix "db" would collide with nothing reserved by itself, but its variable does not:
    assert generate_schema_variable_name("db") == "DATABASE_SCHEMA_DB"
    assert "DATABASE_SCHEMA_DB" not in reserved  # so it is allowed; only exact reserved names clash


# --- DatabaseSecret -------------------------------------------------------------


def _secret(**kw) -> DatabaseSecret:
    base = {"host": "h", "port": 5432, "username": "u", "password": "p", "database": "d", "schema": "proj_dep"}
    base.update(kw)
    return DatabaseSecret(**base)


def test_no_extra_schemas_connection_string_is_unchanged():
    # The default (no extra schemas) must be byte-identical to the historical format.
    secret = _secret()
    data = secret.to_k8s_secret_data()
    assert data["DATABASE_SERVER_FULL"].endswith("?options=--search_path%3Dproj_dep,public")
    # No stray schema variables.
    assert not any(k.startswith("DATABASE_SCHEMA_") for k in data)


def test_extra_schemas_emit_variables_and_aliases():
    secret = _secret(extra_schemas=[("rapportage", "proj_dep_rapportage"), ("audit", "proj_dep_audit")])
    data = secret.to_k8s_secret_data()
    assert data["DATABASE_SCHEMA_RAPPORTAGE"] == "proj_dep_rapportage"
    assert data["APP_DATABASE_SCHEMA_RAPPORTAGE"] == "proj_dep_rapportage"
    assert data["DATABASE_SCHEMA_AUDIT"] == "proj_dep_audit"
    assert data["APP_DATABASE_SCHEMA_AUDIT"] == "proj_dep_audit"
    # The default schema variable still points at the default schema.
    assert data["DATABASE_SCHEMA"] == "proj_dep"


def test_extra_schemas_join_into_search_path_default_first():
    secret = _secret(extra_schemas=[("rapportage", "proj_dep_rapportage"), ("audit", "proj_dep_audit")])
    conn = secret.to_k8s_secret_data()["DATABASE_SERVER_FULL"]
    assert conn.endswith("?options=--search_path%3Dproj_dep,proj_dep_rapportage,proj_dep_audit,public")


# --- manager orchestration ------------------------------------------------------


def _manager() -> DatabaseManager:
    mgr = DatabaseManager(project_manager=SimpleNamespace(), db_host="h", admin_username="a", admin_password="p")
    mgr._postgres_connector = AsyncMock()
    mgr._postgres_connector.create_user.return_value = {"status": "created"}
    return mgr


def test_resolve_extra_schemas_maps_postfix_to_full_name():
    mgr = _manager()
    project_data = {
        "name": "proj",
        "services": [{"name": "postgresql-database", "config": {"schemas": [{"postfix": "rapportage"}]}}],
    }
    pairs = mgr._resolve_extra_schemas(project_data, "proj", "dep-1")
    assert pairs == [("rapportage", "proj_dep_1_rapportage")]


def test_resolve_extra_schemas_skips_marked():
    mgr = _manager()
    project_data = {
        "name": "proj",
        "services": [
            {
                "name": "postgresql-database",
                "config": {"schemas": [{"postfix": "keep"}, {"postfix": "gone", "marked-for-deletion": True}]},
            }
        ],
    }
    pairs = mgr._resolve_extra_schemas(project_data, "proj", "dep")
    assert [p for p, _ in pairs] == ["keep"]


@pytest.mark.asyncio
async def test_readonly_role_granted_on_every_schema():
    mgr = _manager()
    mgr._get_existing_database_credentials_from_k8s = AsyncMock(return_value=None)
    schemas = ["proj_dep", "proj_dep_rapportage", "proj_dep_audit"]

    ro_username, _ = await mgr._ensure_readonly_user(
        deployment_name="dep",
        deployment={"name": "dep"},
        main_username="proj_dep",
        database="proj_dep",
        schemas=schemas,
    )

    assert ro_username == "proj_dep_ro"
    granted = [call.args[1] for call in mgr._postgres_connector.grant_readonly_on_schema.call_args_list]
    assert granted == schemas
    # search_path set once over the full ordered list.
    mgr._postgres_connector.set_role_search_path.assert_awaited_once_with(
        username="proj_dep_ro", database="proj_dep", schemas=schemas
    )


# --- clone path -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_schema_clones_every_extra_schema_through_the_same_pipeline():
    connector = PostgresConnector(host="h", admin_username="a", admin_password="p")

    conn = AsyncMock()
    conn.fetchval.return_value = 1  # source and target databases already exist
    connector._get_or_create_connection = AsyncMock(return_value=conn)
    connector._precreate_extensions_for_clone = AsyncMock(return_value=[])
    connector._execute_pgdump_clone = AsyncMock()

    result = await connector.clone_schema(
        source_database="proj_dep",
        target_database="proj_dep_v2",
        source_schema="proj_dep",
        target_schema="proj_dep_v2",
        target_owner="proj_dep",
        target_owner_password="pw",
        additional_schemas=[("proj_dep_rapportage", "proj_dep_rapportage")],
    )

    assert result["status"] == "success"
    # One pipeline run for the default schema, one per extra schema.
    dumped = [(kw["source_schema"], kw["target_schema"]) for _, kw in connector._execute_pgdump_clone.call_args_list]
    assert ("proj_dep", "proj_dep_v2") in dumped
    assert ("proj_dep_rapportage", "proj_dep_rapportage") in dumped
    assert len(dumped) == 2


@pytest.mark.asyncio
async def test_clone_schema_without_extras_is_single_pipeline_run():
    connector = PostgresConnector(host="h", admin_username="a", admin_password="p")
    conn = AsyncMock()
    conn.fetchval.return_value = 1
    connector._get_or_create_connection = AsyncMock(return_value=conn)
    connector._precreate_extensions_for_clone = AsyncMock(return_value=[])
    connector._execute_pgdump_clone = AsyncMock()

    await connector.clone_schema(
        source_database="proj_dep",
        target_database="proj_dep_v2",
        source_schema="proj_dep",
        target_schema="proj_dep_v2",
        target_owner="proj_dep",
        target_owner_password="pw",
    )

    assert connector._execute_pgdump_clone.await_count == 1
