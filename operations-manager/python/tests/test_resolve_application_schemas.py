"""``DatabaseManager._resolve_application_schemas`` tegen een gemockte connector (RC-360).

``tests/test_clone_all_schemas.py`` meet deze logica al tegen een echte PostgreSQL 17,
maar dat vraagt ``psql``/``pg_dump`` en een server op poort 5432 (``requires_infra``).
Deze tests draaien zonder infra en dekken de ontdekkingslogica zelf: welke schema's
meegaan, de naam-guard en de samenvalcontrole, en dat er pas gerapporteerd wordt nadat
de validatie geslaagd is.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.connectors.postgres import PostgresConnector, PostgresValidationError, SchemaInventory
from opi.manager.database_manager import DatabaseManager

SOURCE_DB = "proj_production"


def _manager(inventory: SchemaInventory) -> tuple[DatabaseManager, MagicMock]:
    progress_manager = MagicMock()
    progress_manager.add_task = MagicMock(return_value="task-id")
    project_manager = MagicMock(get_progress_manager=MagicMock(return_value=progress_manager))

    manager = DatabaseManager(project_manager=project_manager, db_host="h", admin_username="admin", admin_password="pw")
    # Een echte connector: validate_cloneable_schema_name is pure logica (geen I/O), dus
    # alleen describe_schemas (de enige databaseaanroep) wordt gemockt.
    connector = PostgresConnector(host="h", admin_username="admin", admin_password="pw")
    connector.describe_schemas = AsyncMock(return_value=inventory)
    manager._postgres_connector = connector
    return manager, progress_manager


# --- welke schema's meegaan -----------------------------------------------------------


@pytest.mark.asyncio
async def test_app_schemas_come_along_as_their_own_name_to_name_pair() -> None:
    manager, _ = _manager(
        SchemaInventory(
            schemas=["proj_production", "peers", "contracts"], extension_namespaces=set(), public_objects=[]
        )
    )

    discovered = await manager._resolve_application_schemas(
        source_database=SOURCE_DB, platform_sources={"proj_production"}, platform_targets={"proj_staging"}
    )

    assert sorted(discovered) == [("contracts", "contracts"), ("peers", "peers")]


@pytest.mark.asyncio
async def test_public_and_system_schemas_are_excluded() -> None:
    manager, _ = _manager(
        SchemaInventory(
            schemas=["proj_production", "public", "pg_temp_1", "pg_catalog", "peers"],
            extension_namespaces=set(),
            public_objects=[],
        )
    )

    discovered = await manager._resolve_application_schemas(
        source_database=SOURCE_DB, platform_sources={"proj_production"}, platform_targets={"proj_staging"}
    )

    assert discovered == [("peers", "peers")]


@pytest.mark.asyncio
async def test_extension_namespace_is_excluded() -> None:
    manager, _ = _manager(
        SchemaInventory(
            schemas=["proj_production", "topology", "peers"],
            extension_namespaces={"topology"},
            public_objects=[],
        )
    )

    discovered = await manager._resolve_application_schemas(
        source_database=SOURCE_DB, platform_sources={"proj_production"}, platform_targets={"proj_staging"}
    )

    assert discovered == [("peers", "peers")]


@pytest.mark.asyncio
async def test_a_platform_schema_is_not_rediscovered_as_an_application_schema() -> None:
    """RC-17-extra schema's (en het standaardschema) zitten al in ``extra_clone_pairs``."""
    manager, _ = _manager(
        SchemaInventory(
            schemas=["proj_production", "proj_production_rapportage", "peers"],
            extension_namespaces=set(),
            public_objects=[],
        )
    )

    discovered = await manager._resolve_application_schemas(
        source_database=SOURCE_DB,
        platform_sources={"proj_production", "proj_production_rapportage"},
        platform_targets={"proj_staging", "proj_staging_rapportage"},
    )

    assert discovered == [("peers", "peers")]


@pytest.mark.asyncio
async def test_without_application_schemas_the_result_is_empty() -> None:
    """Zonder app-schema's blijft ``extra_clone_pairs`` precies wat het nu is."""
    manager, progress_manager = _manager(
        SchemaInventory(
            schemas=["proj_production", "proj_production_rapportage"], extension_namespaces=set(), public_objects=[]
        )
    )

    discovered = await manager._resolve_application_schemas(
        source_database=SOURCE_DB,
        platform_sources={"proj_production", "proj_production_rapportage"},
        platform_targets={"proj_staging", "proj_staging_rapportage"},
    )

    assert discovered == []
    progress_manager.add_task.assert_not_called()


# --- de naam-guard ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "Peers",  # hoofdletter
        "peers$x",  # dollarteken
        "select",  # gereserveerd woord
        "a" * 64,  # langer dan 63 tekens
    ],
)
@pytest.mark.asyncio
async def test_a_name_the_platform_cannot_clone_fails_before_anything_is_reported(bad_name: str) -> None:
    manager, progress_manager = _manager(
        SchemaInventory(schemas=["proj_production", bad_name], extension_namespaces=set(), public_objects=[])
    )

    with pytest.raises(PostgresValidationError, match=re.escape(bad_name)):
        await manager._resolve_application_schemas(
            source_database=SOURCE_DB, platform_sources={"proj_production"}, platform_targets={"proj_staging"}
        )

    # Geen enkele melding in de taakvoortgang: een halve kloon is het probleem zelf.
    progress_manager.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_a_name_that_collides_with_a_platform_target_fails() -> None:
    manager, progress_manager = _manager(
        SchemaInventory(schemas=["proj_production", "proj_staging"], extension_namespaces=set(), public_objects=[])
    )

    with pytest.raises(PostgresValidationError, match="proj_staging"):
        await manager._resolve_application_schemas(
            source_database=SOURCE_DB, platform_sources={"proj_production"}, platform_targets={"proj_staging"}
        )

    progress_manager.add_task.assert_not_called()


# --- wat de gebruiker ziet --------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovered_schemas_are_reported_in_task_progress() -> None:
    manager, progress_manager = _manager(
        SchemaInventory(schemas=["proj_production", "peers"], extension_namespaces=set(), public_objects=[])
    )

    await manager._resolve_application_schemas(
        source_database=SOURCE_DB, platform_sources={"proj_production"}, platform_targets={"proj_staging"}
    )

    subjects = [
        call.kwargs.get("subject", call.args[-1] if call.args else None)
        for call in progress_manager.add_task.call_args_list
    ]
    assert any("peers" in str(subject) for subject in subjects)


@pytest.mark.asyncio
async def test_public_objects_left_behind_are_reported_by_name() -> None:
    manager, progress_manager = _manager(
        SchemaInventory(schemas=["proj_production"], extension_namespaces=set(), public_objects=["oud_logboek"])
    )

    await manager._resolve_application_schemas(
        source_database=SOURCE_DB, platform_sources={"proj_production"}, platform_targets={"proj_staging"}
    )

    subjects = [
        call.kwargs.get("subject", call.args[-1] if call.args else None)
        for call in progress_manager.add_task.call_args_list
    ]
    assert any("oud_logboek" in str(subject) for subject in subjects)


# --- validate_cloneable_schema_name zelf -------------------------------------------------


def test_validate_cloneable_schema_name_accepts_lowercase_snake_case() -> None:
    assert PostgresConnector.validate_cloneable_schema_name("peers_v2") == "peers_v2"


def test_validate_cloneable_schema_name_rejects_uppercase() -> None:
    with pytest.raises(PostgresValidationError, match="Peers"):
        PostgresConnector.validate_cloneable_schema_name("Peers")


def test_validate_cloneable_schema_name_rejects_dollar_sign() -> None:
    with pytest.raises(PostgresValidationError, match=r"peers\$x"):
        PostgresConnector.validate_cloneable_schema_name("peers$x")
