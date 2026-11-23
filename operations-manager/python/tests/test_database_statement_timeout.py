"""The read-only role of a deployment has a statement_timeout (RC-519).

That role is what a person gets in the database console, and an ad-hoc query there is the
classic noisy neighbour on the shared server. Measured in two places: the connector (the
statement, and what it refuses) and the manager (every processing round sets it, so an
existing role gets it too).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from opi.connectors.postgres import PostgresConnector, PostgresExecutionError, PostgresValidationError
from opi.manager.database_manager import READONLY_STATEMENT_TIMEOUT, DatabaseManager


def _connector() -> tuple[PostgresConnector, AsyncMock]:
    connector = PostgresConnector("rig-db-rw", "postgres", "password")
    conn = AsyncMock()
    connector._get_or_create_connection = AsyncMock(return_value=conn)  # type: ignore[method-assign]
    return connector, conn


# --- the connector ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_setting_lands_as_one_alter_role() -> None:
    connector, conn = _connector()
    await connector.set_role_setting("proj_test_ro", "statement_timeout", "5min")

    assert [call.args[0] for call in conn.execute.await_args_list] == [
        "ALTER ROLE \"proj_test_ro\" SET statement_timeout = '5min'"
    ]


@pytest.mark.asyncio
async def test_a_setting_outside_the_allowlist_is_refused() -> None:
    """ALTER ROLE ... SET takes no bind parameter, so only the allowlist keeps DDL honest."""
    connector, conn = _connector()
    with pytest.raises(PostgresValidationError, match="not one OPI sets"):
        await connector.set_role_setting("proj_test_ro", "session_replication_role", "replica")

    conn.execute.assert_not_awaited()


@pytest.mark.parametrize("value", ["5 min", "5min; DROP DATABASE x", "", "'5min'", "0", "-1", "default"])
@pytest.mark.asyncio
async def test_a_value_that_does_not_hold_up_is_refused(value: str) -> None:
    connector, conn = _connector()
    with pytest.raises(PostgresValidationError):
        await connector.set_role_setting("proj_test_ro", "statement_timeout", value)

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_invalid_role_name_never_reaches_ddl() -> None:
    connector, conn = _connector()
    with pytest.raises(PostgresValidationError):
        await connector.set_role_setting('proj"; DROP DATABASE x', "statement_timeout", "5min")

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_database_error_becomes_a_connector_error() -> None:
    connector, conn = _connector()
    conn.execute.side_effect = asyncpg.InsufficientPrivilegeError("permission denied")
    with pytest.raises(PostgresExecutionError, match="permission denied"):
        await connector.set_role_setting("proj_test_ro", "statement_timeout", "5min")


# --- the manager ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_processing_round_sets_the_timeout_on_the_read_only_role() -> None:
    """Also for a role that already exists: that is how a deployment from before gets it."""
    settings_set: list[tuple[str, str, str]] = []

    async def set_role_setting(username: str, setting: str, value: str) -> None:
        settings_set.append((username, setting, value))

    async def noop(*args: Any, **kwargs: Any) -> dict[str, str]:
        return {"status": "success"}

    project_manager = SimpleNamespace(get_progress_manager=lambda: None)
    mgr = DatabaseManager(project_manager=project_manager, db_host="h", admin_username="a", admin_password="p")
    mgr._postgres_connector = SimpleNamespace(  # type: ignore[assignment]
        create_user=AsyncMock(return_value={"status": "exists"}),
        set_connection_limit=AsyncMock(return_value={"status": "unchanged", "previous": 20, "connection_limit": 20}),
        update_user_password=noop,
        grant_readonly_on_schema=noop,
        set_role_search_path=noop,
        set_role_setting=set_role_setting,
    )

    with (
        patch.object(
            DatabaseManager,
            "_get_existing_database_credentials_from_k8s",
            AsyncMock(return_value=SimpleNamespace(ro_password="DummyPassword123ro")),
        ),
        patch.object(DatabaseManager, "_test_database_connection", AsyncMock(return_value=True)),
    ):
        ro_username, _ro_password = await mgr._ensure_readonly_user(
            deployment_name="test",
            deployment={"name": "test"},
            main_username="proj_test",
            database="proj_test",
            schemas=["proj_test"],
            connection_limit=20,
            db_host="h",
        )

    assert settings_set == [(ro_username, "statement_timeout", READONLY_STATEMENT_TIMEOUT)]
