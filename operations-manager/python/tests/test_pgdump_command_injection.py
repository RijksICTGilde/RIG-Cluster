"""Regression test for the pg_dump external-clone command injection (Vuln 1).

The clone-database-from-external endpoint accepts source host/username/password
as free-form strings. These reach PostgresConnector._execute_pgdump_clone, which
previously built a shell string via f-string interpolation and ran it with
asyncio.create_subprocess_shell -- allowing a project owner to execute arbitrary
commands in the OPI pod via a payload like `$(touch sentinel)`.

This test drives the exploit directly: it calls _execute_pgdump_clone with an
injection payload in source_host and asserts the side-effect command does NOT
execute. The pg_dump/psql binaries are absent in CI, so the clone fails either
way -- the security assertion is purely "did the injected command run".

Red (vulnerable code, create_subprocess_shell): the sentinel file IS created
because `$(touch ...)` is interpreted by the shell before pg_dump runs.
Green (fixed code, create_subprocess_exec + env): the payload is passed as
literal data, never interpreted, the sentinel file is never created.
"""

import contextlib
import uuid
from pathlib import Path

from opi.connectors.postgres import PostgresConnector


async def test_pgdump_clone_does_not_execute_injected_command(tmp_path: Path) -> None:
    sentinel = tmp_path / f"pwned-{uuid.uuid4().hex}"
    assert not sentinel.exists()

    # A shell-substitution payload in an unvalidated source connection field.
    # Under the vulnerable implementation this is interpreted by /bin/sh and
    # creates the sentinel file. Under the fixed implementation it is passed
    # to pg_dump as a literal PGHOST value via the environment and never run.
    payload = f"localhost$(touch {sentinel})"

    connector = PostgresConnector("target-host", "admin", "Adminpass123")

    # The clone will fail (no reachable database / no pg_dump binary in CI).
    # We only care that the injected command did not execute as a side effect,
    # so the failure mode itself is irrelevant and intentionally swallowed.
    with contextlib.suppress(Exception):
        await connector._execute_pgdump_clone(
            source_host=payload,
            source_port=5432,
            source_username="src",
            source_password="Srcpass123",
            source_database="srcdb",
            source_schema="public",
            target_host="target-host",
            target_port=5432,
            target_username="admin",
            target_password="Adminpass123",
            target_database="tgtdb",
            target_schema="public",
            target_owner="owner",
            target_owner_password="Ownerpass123",
            force_clone=True,
            schema_precreated=False,
        )

    assert not sentinel.exists(), (
        "Command injection: the payload in source_host was executed by a shell. "
        "source connection params must be passed via env to create_subprocess_exec, "
        "never interpolated into a shell command string."
    )


async def test_pgdump_clone_injection_via_password(tmp_path: Path) -> None:
    """source_password only passes a weak complexity check and must never reach a shell."""
    sentinel = tmp_path / f"pwned-{uuid.uuid4().hex}"
    # Payload satisfies _validate_password (has a letter and a digit, length >= 8)
    # yet contains shell metacharacters.
    payload = f"Aa1;touch {sentinel};"

    connector = PostgresConnector("target-host", "admin", "Adminpass123")

    with contextlib.suppress(Exception):
        await connector._execute_pgdump_clone(
            source_host="localhost",
            source_port=5432,
            source_username="src",
            source_password=payload,
            source_database="srcdb",
            source_schema="public",
            target_host="target-host",
            target_port=5432,
            target_username="admin",
            target_password="Adminpass123",
            target_database="tgtdb",
            target_schema="public",
            target_owner="owner",
            target_owner_password="Ownerpass123",
            force_clone=True,
            schema_precreated=False,
        )

    assert not sentinel.exists(), "Command injection via source_password reached a shell"
