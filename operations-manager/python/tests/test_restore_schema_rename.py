"""A restore must rename the schema the APPLICATION uses, and nothing else (RC-121).

OPI puts the generation in the database name AND in the default schema name
(``db_schema = db_database``), so restoring into a new generation means renaming that
one schema. The restore pod used to find it with::

    pg_restore --list "$DUMP_PATH" | grep " SCHEMA - " | head -1 | awk '{print $6}'

which assumes the dump holds exactly one schema. Since RC-17 it does not: extra schemas
(``{project}_{deployment}_{postfix}``) live in the same database and are dumped along
with it. And ``pg_restore --list`` sorts schemas ALPHABETICALLY, not by creation order --
pinned by ``test_toc_lists_schemas_alphabetically`` below. So a project with an extra
schema, restored to its second generation, renamed ``amt_prod_rapportage`` (r < v) to
``amt_prod_v3``: the application then read the reporting tables, its own data sat unused
in ``amt_prod_v2``, and the extra schema was gone. The restore reported success.

What is pinned here:

* the source schema is NAMED by the platform (``generate_database_name``) and passed in
  as an env var; the pod never derives it from the dump's ordering;
* extra schemas are left alone -- they carry no generation, so their name is already
  right in the target database;
* the target schema is only dropped when it is EMPTY, and with RESTRICT rather than
  CASCADE, so a restore can no longer destroy the data it just restored;
* both identifiers are quoted and refused unless they match the platform naming rule;
* what happened is in the log: which schema was renamed, and which were left alone.

The tests marked ``requires_infra`` run the shipped shell block against a real
PostgreSQL. Point them at one with::

    docker run -d --rm --name pg -e POSTGRES_PASSWORD=pw postgres:16
    RESTORE_SCHEMA_TEST_DSN=postgresql://postgres:pw@<ip>:5432/postgres \\
      uv run pytest tests/test_restore_schema_rename.py -m requires_infra -o addopts=""
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from opi.manager.backup.base import BackupConfig, SnapshotInfo
from opi.manager.backup.database_backup import DatabaseBackupManager

_MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"
_TEMPLATE = "restore-database-pod.yaml.jinja"

# The part of the pod script this task is about. Everything from here on only needs the
# TARGET_DB_* env vars, SOURCE_SCHEMA and a DUMP_PATH, which is what lets the
# requires_infra tests below run the SHIPPED code instead of a copy of it.
_SCHEMA_SECTION_MARKER = "# Which schema in the dump holds"

_RENDER_CONTEXT: dict[str, Any] = {
    "pod_name": "db-restore-test",
    "namespace": "rig-amt",
    "reference_name": "amt-db",
    "target_db_host": "postgres.example",
    "target_db_port": 5432,
    "target_db_name": "amt_prod_v3",
    "target_db_user": "amt_prod",
    "target_db_password": "pw",
    "s3_endpoint": "http://minio:9000",
    "s3_bucket": "backups",
    "s3_access_key": "ak",
    "s3_secret_key": "sk",
    "s3_disable_tls": True,
    "backup_prefix": "local/rig-amt",
    "kopia_password": "kp",
    "snapshot_id": "",
    "timeout_seconds": 3600,
    "target_unusable_exit_code": 42,
    "source_schema": "amt_prod_v2",
}


def _render(**overrides: Any) -> dict[str, Any]:
    env = Environment(loader=FileSystemLoader(str(_MANIFESTS_DIR)), undefined=StrictUndefined)
    rendered = env.get_template(_TEMPLATE).render(**{**_RENDER_CONTEXT, **overrides})
    return yaml.safe_load(rendered)


def _script(**overrides: Any) -> str:
    return _render(**overrides)["spec"]["containers"][0]["command"][2]


def _env_value(pod: dict[str, Any], name: str) -> str | None:
    for entry in pod["spec"]["containers"][0]["env"]:
        if entry["name"] == name:
            return entry["value"]
    return None


class TestTheSourceSchemaIsNamedNotGuessed:
    """The platform says which schema is the application's; the dump does not."""

    def test_source_schema_is_passed_in_as_an_env_var(self) -> None:
        pod = _render(source_schema="amt_prod_v2")
        assert _env_value(pod, "SOURCE_SCHEMA") == "amt_prod_v2"

    def test_the_script_never_derives_the_source_schema_from_the_dump_order(self) -> None:
        script = _script()
        section = script[script.index(_SCHEMA_SECTION_MARKER) :]
        assert "SOURCE_SCHEMA=$(" not in script, "the source schema must come from the platform, not from the dump"
        assert "head -1" not in section, "picking the first schema in the TOC is an alphabetical coin flip"
        assert "awk" not in section

    def test_an_unnamed_source_only_proceeds_when_the_dump_carries_the_target_name(self) -> None:
        # No name and no schema with the target's name: refuse rather than guess.
        script = _script(source_schema="")
        assert 'grep -qF " SCHEMA - ${TARGET_DB_NAME} "' in script
        assert "cannot be established" in script


class TestTheBackupSaysWhichDatabaseItDumped:
    """The dump's own tag beats any reconstruction of the source name."""

    def test_the_backup_pod_tags_the_database_it_dumps(self) -> None:
        env = Environment(loader=FileSystemLoader(str(_MANIFESTS_DIR)), undefined=StrictUndefined)
        source = env.loader.get_source(env, "backup-database-pod.yaml.jinja")[0]  # type: ignore[union-attr]
        assert '--tags="source_database:{{ db_name }}"' in source, (
            "without this tag the restore has to reconstruct the source name, "
            "which the cluster measurement showed can be wrong"
        )

    @pytest.mark.parametrize(
        "template",
        ["backup-database-pod.yaml.jinja", "restore-database-pod.yaml.jinja"],
    )
    def test_no_comment_interrupts_a_continued_command(self, template: str) -> None:
        """A `#` line between backslash-continued lines silently ends the command.

        Measured on the cluster, not reasoned about: an explanatory comment placed
        between two `--tags=... \\` lines made the shell run the next line as its own
        command and the backup died with
        `/bin/sh: --tags=source_database:...: not found`.
        """
        env = Environment(loader=FileSystemLoader(str(_MANIFESTS_DIR)), undefined=StrictUndefined)
        source = env.loader.get_source(env, template)[0]  # type: ignore[union-attr]
        lines = source.splitlines()
        offenders = [
            (number, line)
            for number, line in enumerate(lines[1:], start=2)
            if line.lstrip().startswith("#") and lines[number - 2].rstrip().endswith("\\")
        ]
        assert not offenders, f"comment inside a continued command in {template}: {offenders}"


class TestTheDropIsSafe:
    """Only the empty target schema created at database setup may be dropped."""

    def test_the_drop_is_restrict_and_never_cascade(self) -> None:
        script = _script()
        statements = [
            line for line in script.splitlines() if "DROP SCHEMA" in line and not line.lstrip().startswith("#")
        ]
        assert statements, "the rename still has to drop the empty target schema"
        assert not any("CASCADE" in line for line in statements), (
            "CASCADE dropped data that pg_restore had just written"
        )
        assert 'DROP SCHEMA IF EXISTS \\"${TARGET_DB_NAME}\\" RESTRICT;' in script

    def test_a_non_empty_target_schema_stops_the_restore(self) -> None:
        script = _script()
        assert "TARGET_OBJECTS" in script
        assert "refusing to drop it" in script

    def test_both_identifiers_are_quoted_and_checked(self) -> None:
        script = _script()
        assert 'ALTER SCHEMA \\"${SOURCE_SCHEMA}\\" RENAME TO \\"${TARGET_DB_NAME}\\";' in script
        assert 'check_identifier "source schema" "${SOURCE_SCHEMA}"' in script
        assert 'check_identifier "target schema" "${TARGET_DB_NAME}"' in script

    def test_the_log_says_what_was_renamed_and_what_was_left_alone(self) -> None:
        script = _script()
        assert 'echo "Renamed ${SOURCE_SCHEMA} to ${TARGET_DB_NAME}"' in script
        assert "Schemas left untouched" in script


def _manager() -> DatabaseBackupManager:
    kubectl = MagicMock()
    kubectl.run_command = AsyncMock(return_value=("", "", 0))
    # The one piece of the connector the restore path really uses: Jinja rendering.
    kubectl.template_manifest = lambda content, variables: (
        Environment(undefined=StrictUndefined).from_string(content).render(**variables)
    )

    with patch("opi.manager.backup.base.KubectlConnector", return_value=kubectl):
        manager = DatabaseBackupManager(
            BackupConfig(
                s3_endpoint="http://minio:9000",
                s3_bucket="backups",
                s3_access_key="ak",
                s3_secret_key="sk",
            )
        )
    manager.kubectl = kubectl
    manager._derive_backup_key = AsyncMock(return_value="kp")  # type: ignore[method-assign]
    manager._wait_for_pod = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._cleanup_pod = AsyncMock()  # type: ignore[method-assign]
    manager.lock = MagicMock()
    # No snapshot metadata unless a test provides it.
    manager.list_database_snapshots = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return manager


def _applied_pod(manager: DatabaseBackupManager) -> dict[str, Any]:
    call = manager.kubectl.run_command.await_args
    return yaml.safe_load(call.kwargs["stdin_input"])


_SNAPSHOT = SnapshotInfo(
    snapshot_id="k1",
    pvc_name="amt-db",
    timestamp="2026-08-01T10:00:00Z",
    project_name="amt",
    deployment_name="prod",
    generation=2,
    resource_type="database",
    source_database="amt_prod_v2",
)

#: A snapshot from before the source_database tag existed: only the metadata to compose from.
_SNAPSHOT_WITHOUT_TAG = SnapshotInfo(
    snapshot_id="k1",
    pvc_name="amt-db",
    timestamp="2026-08-01T10:00:00Z",
    project_name="amt",
    deployment_name="prod",
    generation=2,
    resource_type="database",
)


class TestTheManagerNamesTheSourceSchema:
    @pytest.mark.asyncio
    async def test_the_caller_supplied_name_reaches_the_pod(self) -> None:
        manager = _manager()

        result = await manager._restore_database(
            cluster="local",
            namespace="rig-amt",
            reference_name="amt-db",
            target_database_host="postgres.example",
            target_database_port=5432,
            target_database_name="amt_prod_v3",
            target_database_user="amt_prod",
            target_database_password="pw",
            snapshot_id="k1",
            project_name="amt",
            source_database_name="amt_prod_v2",
        )

        assert result.success
        assert _env_value(_applied_pod(manager), "SOURCE_SCHEMA") == "amt_prod_v2"

    @pytest.mark.asyncio
    async def test_the_snapshots_own_tag_beats_what_the_caller_says(self) -> None:
        """Measured on the cluster: the caller's idea of the previous generation can lag.

        A second generation restore passed ``amt_prod`` while the backup had dumped
        ``amt_prod_v2``. The backup pod tags the database it really dumped, so that tag
        wins.
        """
        manager = _manager()
        manager.list_database_snapshots = AsyncMock(return_value=[_SNAPSHOT])  # type: ignore[method-assign]

        await manager._restore_database(
            cluster="local",
            namespace="rig-amt",
            reference_name="amt-db",
            target_database_host="postgres.example",
            target_database_port=5432,
            target_database_name="amt_prod_v3",
            target_database_user="amt_prod",
            target_database_password="pw",
            snapshot_id="k1",
            project_name="amt",
            source_database_name="amt_prod",
        )

        assert _env_value(_applied_pod(manager), "SOURCE_SCHEMA") == "amt_prod_v2"

    @pytest.mark.asyncio
    async def test_without_a_tag_or_a_supplied_name_it_is_composed_from_the_snapshot(self) -> None:
        """Snapshots from before the tag existed still have project, deployment and generation."""
        manager = _manager()
        manager.list_database_snapshots = AsyncMock(return_value=[_SNAPSHOT_WITHOUT_TAG])  # type: ignore[method-assign]

        await manager._restore_database(
            cluster="local",
            namespace="rig-amt",
            reference_name="amt-db",
            target_database_host="postgres.example",
            target_database_port=5432,
            target_database_name="amt_prod_v3",
            target_database_user="amt_prod",
            target_database_password="pw",
            snapshot_id="k1",
            project_name="amt",
        )

        assert _env_value(_applied_pod(manager), "SOURCE_SCHEMA") == "amt_prod_v2"

    @pytest.mark.asyncio
    async def test_an_unresolvable_source_leaves_the_name_empty(self) -> None:
        """Nothing is invented: the pod then refuses unless there is nothing to rename."""
        manager = _manager()

        await manager._restore_database(
            cluster="local",
            namespace="rig-amt",
            reference_name="amt-db",
            target_database_host="postgres.example",
            target_database_port=5432,
            target_database_name="amt_prod_v3",
            target_database_user="amt_prod",
            target_database_password="pw",
            project_name="amt",
        )

        assert _env_value(_applied_pod(manager), "SOURCE_SCHEMA") == ""

    @pytest.mark.asyncio
    async def test_a_name_outside_the_naming_rule_is_refused_before_any_pod_runs(self) -> None:
        manager = _manager()

        result = await manager._restore_database(
            cluster="local",
            namespace="rig-amt",
            reference_name="amt-db",
            target_database_host="postgres.example",
            target_database_port=5432,
            target_database_name="amt_prod_v3",
            target_database_user="amt_prod",
            target_database_password="pw",
            project_name="amt",
            source_database_name='amt"; DROP SCHEMA public --',
        )

        assert not result.success
        assert "naming rule" in (result.error or "")
        manager.kubectl.run_command.assert_not_awaited()


class TestTheGenerationRestorePassesThePreviousGeneration:
    @pytest.mark.asyncio
    async def test_the_previous_generation_database_is_named_as_the_source(self) -> None:
        """The restore into generation N+1 knows exactly which database the dump came from."""
        from opi.api.restore_router import _restore_database_with_versioning

        backup_manager = MagicMock()
        backup_manager.restore_database = AsyncMock(return_value=MagicMock(success=True, error=None))

        postgres = MagicMock()
        postgres.create_user = AsyncMock(return_value={"status": "exists"})
        postgres.create_database = AsyncMock(return_value={"status": "created"})
        postgres.create_schema = AsyncMock(return_value={"status": "created"})
        postgres.close = AsyncMock()

        handler = MagicMock()
        handler.get_database_generation.return_value = 2

        secret = MagicMock()
        secret.password = "existing"

        with (
            patch("opi.api.restore_router.create_database_backup_manager", return_value=backup_manager),
            patch("opi.connectors.postgres.create_postgres_connector", return_value=postgres),
            patch("opi.core.cluster_config.get_database_server", return_value="postgres.example"),
            patch("opi.api.restore_router.create_kubectl_connector", return_value=MagicMock()),
            patch("opi.utils.secrets.DatabaseSecret.get_data", AsyncMock(return_value=secret)),
        ):
            result = await _restore_database_with_versioning(
                project_name="amt",
                deployment_name="prod",
                component_name="app",
                reference_name="amt-db",
                snapshot_id="k1",
                deployment_cluster="local",
                namespace="rig-amt",
                project_data={},
                project_file_handler=handler,
            )

        assert result["success"]
        call = backup_manager.restore_database.await_args.kwargs
        assert call["target_database_name"] == "amt_prod_v3"
        assert call["source_database_name"] == "amt_prod_v2"


# =========================================================================
# Against a real PostgreSQL
# =========================================================================


def _dsn() -> str | None:
    dsn = os.environ.get("RESTORE_SCHEMA_TEST_DSN")
    if not dsn:
        return None
    if not all(shutil.which(tool) for tool in ("psql", "pg_dump", "pg_restore")):
        return None
    return dsn


requires_postgres = pytest.mark.skipif(_dsn() is None, reason="needs RESTORE_SCHEMA_TEST_DSN and the psql client tools")


@pytest.fixture
def pg() -> Any:
    """A live PostgreSQL to dump from and restore into."""
    parsed = urlparse(_dsn() or "")
    libpq_env = {
        **os.environ,
        "PGHOST": parsed.hostname or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": parsed.username or "postgres",
        "PGPASSWORD": parsed.password or "",
    }

    class Postgres:
        env = libpq_env

        def sql(self, database: str, statement: str) -> str:
            done = subprocess.run(
                ["psql", "-d", database, "-v", "ON_ERROR_STOP=1", "-Atc", statement],
                env=libpq_env,
                capture_output=True,
                text=True,
                check=True,
            )
            return done.stdout.strip()

        def recreate(self, database: str) -> None:
            self.sql("postgres", f"DROP DATABASE IF EXISTS {database}")
            self.sql("postgres", f"CREATE DATABASE {database}")

        def dump(self, database: str, path: Path) -> None:
            subprocess.run(
                ["pg_dump", "--format=custom", "-d", database, "-f", str(path)],
                env=libpq_env,
                check=True,
                capture_output=True,
            )

        def toc_schemas(self, path: Path) -> list[str]:
            done = subprocess.run(["pg_restore", "--list", str(path)], env=libpq_env, capture_output=True, text=True)
            return [line.split(" SCHEMA - ")[1].split()[0] for line in done.stdout.splitlines() if " SCHEMA - " in line]

        def relations(self, database: str) -> list[str]:
            listing = self.sql(
                database,
                "SELECT n.nspname || '.' || c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema' "
                "AND c.relkind = 'r' ORDER BY 1",
            )
            return listing.splitlines() if listing else []

    return Postgres()


def _run_schema_section(pg: Any, *, target: str, source_schema: str, dump: Path) -> subprocess.CompletedProcess[str]:
    """Run the SHIPPED schema-handling block of the restore pod against a real database."""
    script = _script()
    section = script[script.index(_SCHEMA_SECTION_MARKER) :]
    return subprocess.run(
        ["sh", "-c", "set -e\n" + section],
        env={
            **pg.env,
            "TARGET_DB_HOST": pg.env["PGHOST"],
            "TARGET_DB_PORT": pg.env["PGPORT"],
            "TARGET_DB_USER": pg.env["PGUSER"],
            "TARGET_DB_PASSWORD": pg.env["PGPASSWORD"],
            "TARGET_DB_NAME": target,
            "SOURCE_SCHEMA": source_schema,
            "DUMP_PATH": str(dump),
        },
        capture_output=True,
        text=True,
    )


def _second_generation_dump(pg: Any, tmp_path: Path) -> Path:
    """A generation-2 database: the default schema plus an extra schema, dumped whole.

    The extra schema is created FIRST, so its OID is lower than the default schema's.
    If the choice followed creation order it would be safe; it does not.
    """
    pg.recreate("amt_prod_v2")
    pg.sql("amt_prod_v2", "CREATE SCHEMA amt_prod_rapportage")
    pg.sql("amt_prod_v2", "CREATE TABLE amt_prod_rapportage.cijfers (id int)")
    pg.sql("amt_prod_v2", "CREATE SCHEMA amt_prod_v2")
    pg.sql("amt_prod_v2", "CREATE TABLE amt_prod_v2.klanten (id int)")

    dump = tmp_path / "amt_prod_v2.dump"
    pg.dump("amt_prod_v2", dump)
    return dump


@pytest.mark.requires_infra
@requires_postgres
def test_toc_lists_schemas_alphabetically_not_in_creation_order(pg: Any, tmp_path: Path) -> None:
    """The fact the old code assumed away: the TOC order says nothing about which is which."""
    dump = _second_generation_dump(pg, tmp_path)

    oids = pg.sql(
        "amt_prod_v2",
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'amt\\_%' ORDER BY oid",
    ).splitlines()
    assert oids == ["amt_prod_rapportage", "amt_prod_v2"], "the extra schema was created first"

    assert pg.toc_schemas(dump) == ["amt_prod_rapportage", "amt_prod_v2"]

    # And with the order flipped, creation order does NOT follow: it is the alphabet.
    pg.recreate("order_check")
    pg.sql("order_check", "CREATE SCHEMA zzz_first")
    pg.sql("order_check", "CREATE SCHEMA aaa_second")
    flipped = tmp_path / "order_check.dump"
    pg.dump("order_check", flipped)
    assert pg.toc_schemas(flipped) == ["aaa_second", "zzz_first"]


@pytest.mark.requires_infra
@requires_postgres
def test_second_generation_restore_moves_the_application_schema_only(pg: Any, tmp_path: Path) -> None:
    """The scenario that reached production: gen 2 -> gen 3 with an extra schema."""
    dump = _second_generation_dump(pg, tmp_path)

    # The target as database setup leaves it: the database plus an empty default schema.
    pg.recreate("amt_prod_v3")
    pg.sql("amt_prod_v3", "CREATE SCHEMA amt_prod_v3")

    done = _run_schema_section(pg, target="amt_prod_v3", source_schema="amt_prod_v2", dump=dump)
    assert done.returncode == 0, done.stdout + done.stderr

    # The application's own tables are under the name it reads (DATABASE_SCHEMA), and
    # the extra schema still has the name DATABASE_SCHEMA_RAPPORTAGE points at.
    assert pg.relations("amt_prod_v3") == ["amt_prod_rapportage.cijfers", "amt_prod_v3.klanten"]
    assert "Renamed amt_prod_v2 to amt_prod_v3" in done.stdout
    assert "Schemas left untouched: amt_prod_rapportage" in done.stdout


@pytest.mark.requires_infra
@requires_postgres
def test_a_filled_target_schema_stops_the_restore_instead_of_dropping_it(pg: Any, tmp_path: Path) -> None:
    """A dump carrying the target's own name used to have its data dropped by CASCADE."""
    pg.recreate("bron")
    pg.sql("bron", "CREATE SCHEMA amt_prod")
    pg.sql("bron", "CREATE TABLE amt_prod.klanten (id int)")
    pg.sql("bron", "CREATE SCHEMA aaa_werkmap")
    pg.sql("bron", "CREATE TABLE aaa_werkmap.tmp (id int)")
    dump = tmp_path / "bron.dump"
    pg.dump("bron", dump)

    pg.recreate("amt_prod")
    pg.sql("amt_prod", "CREATE SCHEMA amt_prod")

    # A source name that would make the restore rename the wrong schema over a target
    # schema that pg_restore has just filled.
    done = _run_schema_section(pg, target="amt_prod", source_schema="aaa_werkmap", dump=dump)

    assert done.returncode == 1
    assert "refusing to drop it" in done.stdout
    assert "amt_prod.klanten" in pg.relations("amt_prod"), "the restored data must still be there"
