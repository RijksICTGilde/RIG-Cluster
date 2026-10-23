"""De schemanaam in ``_execute_pgdump_clone`` haalt nooit de SQL-tekst (S608, RC-310).

De twee schema-bestaancontroles en de ``DROP``/``ALTER SCHEMA``-aanroepen in deze functie
zetten de schemanaam met een f-string in de query: ``psql -c`` interpoleert geen
psql-variabelen, dus ``-v schema=...`` plus ``:'schema'`` is daar geen alternatief. Wat de
query beschermt is de validatie die nu VOOR stap 1 staat: een naam die niet aan de
PostgreSQL-identifierregels voldoet geeft een ``PostgresValidationError`` voordat het
eerste proces start.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.postgres import PostgresConnector


@pytest.mark.parametrize("field", ["source_schema", "target_schema"])
async def test_a_quote_in_a_schema_name_spawns_no_process(field: str) -> None:
    connector = PostgresConnector("target-host", "admin", "Adminpass123")

    kwargs: dict[str, object] = {
        "source_host": "source-host",
        "source_username": "src",
        "source_password": "Srcpass123",
        "source_database": "srcdb",
        "target_host": "target-host",
        "target_username": "admin",
        "target_password": "Adminpass123",
        "target_database": "tgtdb",
        "target_owner": "owner",
        "target_owner_password": "Ownerpass123",
        "schema_pairs": [("ok_schema", "ok_target")],
        "force_clone": False,
    }
    paar = {"source_schema": ("bad'schema", "ok_target"), "target_schema": ("ok_schema", "bad'schema")}[field]
    kwargs["schema_pairs"] = [paar]

    # De PostgresValidationError wordt door de except van de functie in een generieke
    # Exception gewikkeld; de melding komt er onveranderd uit.
    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
        pytest.raises(Exception, match="contains invalid characters"),
    ):
        await connector._execute_pgdump_clone(**kwargs)  # type: ignore[arg-type]

    assert spawn.call_count == 0, f"psql werd gestart met een ongeldige {field}"


async def test_the_schema_existence_checks_send_sql_psql_can_execute() -> None:
    """De controlequery moet de naam zelf bevatten, geen psql-variabele.

    ``psql -c`` stuurt de string door zonder zijn eigen lexer, dus een ``:'schema'`` blijft
    letterlijk staan: de query is dan een syntaxfout en de controle leest altijd leeg, wat
    de twee guards in stap 1 en 3 dood maakt.
    """
    source_schema = "bron_schema"
    target_schema = "doel_schema"
    captured: list[list[str]] = []

    async def record(*cmd: str, **_kwargs: object) -> AsyncMock:
        captured.append(list(cmd))
        process = AsyncMock()
        process.communicate = AsyncMock(return_value=(b"", b""))
        process.returncode = 0
        return process

    connector = PostgresConnector("target-host", "admin", "Adminpass123")

    # Strandt verderop op het ontbreken van pg_dump; de controles in stap 1 en 3 zijn dan al gedaan.
    with contextlib.suppress(Exception), patch("asyncio.create_subprocess_exec", side_effect=record):
        await connector._execute_pgdump_clone(
            source_host="source-host",
            source_username="src",
            source_password="Srcpass123",
            source_database="srcdb",
            target_host="target-host",
            target_username="admin",
            target_password="Adminpass123",
            target_database="tgtdb",
            target_owner="owner",
            target_owner_password="Ownerpass123",
            schema_pairs=[(source_schema, target_schema)],
            force_clone=False,
        )

    queries = [cmd[cmd.index("-c") + 1] for cmd in captured if "-c" in cmd]
    checks = [sql for sql in queries if "information_schema.schemata" in sql]
    assert [source_schema in sql for sql in checks].count(True) == 1, f"bronschema niet gecontroleerd: {queries}"
    assert [target_schema in sql for sql in checks].count(True) == 1, f"doelschema niet gecontroleerd: {queries}"
    for sql in checks:
        assert ":'" not in sql, f"psql-variabele in een -c query, die wordt niet geinterpoleerd: {sql}"
