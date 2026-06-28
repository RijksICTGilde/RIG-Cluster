"""Unit tests for the ephemeral database console feature."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml
from opi.connectors.postgres import create_postgres_connector
from opi.core.db_console_reaper import DbConsoleReaper
from opi.core.templates import get_templates
from opi.generation.manifests import render_template
from opi.manager.db_console_manager import (
    DbConsoleManager,
    DbConsoleMode,
    DbConsoleSession,
    DbConsoleTool,
)
from opi.utils import naming
from opi.utils.secrets import DatabaseSecret

# --------------------------------------------------------------------- naming


def test_db_console_naming_helpers():
    name = naming.generate_db_console_name("My-Proj", "Prod", "abcd1234")
    assert name == "dbconsole-my-proj-prod-abcd1234"
    assert len(name) <= 63

    assert naming.generate_db_console_client_id("abcd1234") == "dbconsole-abcd1234"

    host = naming.generate_db_console_hostname("My-Proj", "abcd1234", ".sandbox.rijksapp.dev")
    assert host == "dbconsole-my-proj-abcd1234.sandbox.rijksapp.dev"

    role = naming.generate_db_console_ro_role("my-proj", "prod", "abcd1234")
    assert role == "my_proj_prod_ro_abcd1234"  # underscores, valid SQL identifier
    assert "-" not in role


# ----------------------------------------------------------- auth-wall template


def test_sidecar_uses_email_file_when_configured():
    """With an emails configmap the wall switches to --authenticated-emails-file."""
    out = render_template(
        "sidecar-authorization-wall.yaml.jinja",
        {
            "section": "container",
            "name": "dbconsole-x",
            "namespace": "rig-proj",
            "project": {"name": "proj"},
            "application_port": 8081,
            "hostname": "dbconsole-x.example.com",
            "authorization_wall": {
                "issuer_url": "https://kc/realms/operations-manager",
                "client_id": "dbconsole-x",
                "keycloak_secret_name": "dbconsole-x",
                "cookie_secret_name": "dbconsole-x",
                "authenticated_emails_configmap": "dbconsole-x-emails",
            },
        },
    )
    assert "--authenticated-emails-file=/etc/oauth2-proxy/authenticated-emails/emails.txt" in out
    assert "--email-domain=*" not in out


def test_sidecar_falls_back_to_email_domain_for_normal_deployments():
    """Without the emails configmap var, behaviour is unchanged (backward compatible)."""
    out = render_template(
        "sidecar-authorization-wall.yaml.jinja",
        {
            "section": "container",
            "name": "app",
            "namespace": "rig-proj",
            "project": {"name": "proj"},
            "application_port": 8080,
            "hostname": "app.example.com",
            "authorization_wall": {
                "issuer_url": "https://kc/realms/proj",
                "client_id": "proj-app",
                "keycloak_secret_name": "app-keycloak",
                "cookie_secret_name": "app-oauth2-cookie",
            },
        },
    )
    assert "--email-domain=*" in out
    assert "--authenticated-emails-file" not in out


# ----------------------------------------------------------------- pod template


def _render_pod(tool: str, port: int, args: list[str]) -> dict:
    aw = {
        "issuer_url": "https://kc/realms/operations-manager",
        "client_id": "dbconsole-abcd1234",
        "keycloak_secret_name": "dbconsole-proj-dep-abcd1234",
        "cookie_secret_name": "dbconsole-proj-dep-abcd1234",
        "authenticated_emails_configmap": "dbconsole-proj-dep-abcd1234-emails",
    }
    out = render_template(
        "db-console-pod.yaml.jinja",
        {
            "name": "dbconsole-proj-dep-abcd1234",
            "namespace": "rig-proj",
            "project": {"name": "proj"},
            "cluster": "odcn-production",
            "extra_labels": {"rig.zad/db-console": "abcd1234"},
            "extra_annotations": {"rig.zad/expires-at": "2026-06-26T22:00:00+00:00"},
            "target_deployment": "dep",
            "ttl_seconds": 3600,
            "tool_image": f"image:{tool}",
            "application_port": port,
            "tool_args": args,
            "secret_name": "dbconsole-proj-dep-abcd1234",
            "hostname": "dbconsole-abcd1234.example.com",
            "authorization_wall": aw,
        },
    )
    return yaml.safe_load(out)


def test_pod_renders_valid_yaml_with_auth_wall_and_target_label():
    doc = _render_pod("pgweb", 8081, ["--bind=0.0.0.0", "--listen=8081", "--readonly"])
    assert doc["kind"] == "Pod"
    assert doc["spec"]["restartPolicy"] == "Never"
    assert doc["spec"]["activeDeadlineSeconds"] == 3600
    # Load-bearing: deployment label must be the TARGET deployment for the NetworkPolicy.
    assert doc["spec"]["template"]["metadata"]["labels"] if False else doc["metadata"]["labels"]["deployment"] == "dep"
    # app label stays the console's own name so its Service selects only this pod.
    assert doc["metadata"]["labels"]["app"] == "dbconsole-proj-dep-abcd1234"
    names = [c["name"] for c in doc["spec"]["containers"]]
    assert names == ["console", "authorization-wall"]


@pytest.mark.parametrize(("tool", "port"), [("pgweb", 8081), ("dbgate", 3000)])
def test_pod_oauth2_upstream_matches_tool_port(tool, port):
    doc = _render_pod(tool, port, [])
    wall = next(c for c in doc["spec"]["containers"] if c["name"] == "authorization-wall")
    assert f"--upstream=http://localhost:{port}" in wall["args"]


# ------------------------------------------------------------------ tool config


def _db_secret() -> DatabaseSecret:
    return DatabaseSecret(
        host="rig-db-rw",
        port=5432,
        username="proj_dep",
        password="secretpw1",
        database="proj_dep",
        schema="proj_dep",
    )


def test_tool_config_pgweb_readonly_builds_database_url():
    _image, port, args, secret = DbConsoleManager._tool_config(
        DbConsoleTool.PGWEB, DbConsoleMode.READ_ONLY, _db_secret(), "ro_user", "ro_pw"
    )
    assert port == 8081
    assert "--readonly" in args
    assert "--lock-session" in args  # pins to the single connection; no host switching
    assert secret["DATABASE_URL"].startswith("postgresql://ro_user:ro_pw@rig-db-rw:5432/proj_dep")
    assert "search_path" in secret["DATABASE_URL"]


def test_tool_config_dbgate_single_connection_env():
    _image, port, _args, secret = DbConsoleManager._tool_config(
        DbConsoleTool.DBGATE, DbConsoleMode.READ_WRITE, _db_secret(), "proj_dep", "secretpw1"
    )
    assert port == 3000
    assert secret["CONNECTIONS"] == "con1"
    assert secret["SERVER_con1"] == "rig-db-rw"
    assert secret["USER_con1"] == "proj_dep"
    assert secret["ENGINE_con1"] == "postgres@dbgate-plugin-postgres"
    assert secret["DISABLE_VOLATILE_CONNECTIONS"] == "1"  # no ad-hoc connections
    assert "READONLY_con1" not in secret  # rw mode


def test_tool_config_dbgate_readonly_sets_hint():
    _, _, _, secret = DbConsoleManager._tool_config(
        DbConsoleTool.DBGATE, DbConsoleMode.READ_ONLY, _db_secret(), "ro_user", "ro_pw"
    )
    assert secret["READONLY_con1"] == "1"


# --------------------------------------------------------------- realm parsing


def test_zad_realm_parsed_from_discovery_url(monkeypatch):
    monkeypatch.setattr(
        "opi.manager.db_console_manager.settings.OIDC_DISCOVERY_URL",
        "https://keycloak.example/realms/operations-manager/.well-known/openid-configuration",
    )
    assert DbConsoleManager._zad_realm() == "operations-manager"
    assert DbConsoleManager._issuer_url() == "https://keycloak.example/realms/operations-manager"


def test_zad_realm_raises_without_discovery_url(monkeypatch):
    from opi.manager.db_console_manager import DbConsoleError

    monkeypatch.setattr("opi.manager.db_console_manager.settings.OIDC_DISCOVERY_URL", None)
    with pytest.raises(DbConsoleError):
        DbConsoleManager._zad_realm()


# --------------------------------------------- modal body renders via ROOS engine


def _fake_request():
    return SimpleNamespace(state=SimpleNamespace(csrf_token="tok"), scope={"type": "http"}, headers={})


def _render_modal_html(**ctx) -> str:
    tmpl = get_templates().get_template("project-details/_db-console-modal.html.j2")
    return tmpl.render(request=_fake_request(), **ctx)


def test_modal_start_form_renders_through_roos():
    html = _render_modal_html(
        project_name="proj",
        deployment_name="dep",
        session=None,
        state="none",
        error=None,
        ttl_seconds=3600,
        enabled=True,
    )
    assert "Console starten" in html
    assert "/projects/proj/db-console" in html


def test_modal_starting_state_polls_status():
    html = _render_modal_html(
        project_name="proj",
        deployment_name="dep",
        session=None,
        state="starting",
        error=None,
        ttl_seconds=3600,
        enabled=True,
    )
    assert "opgestart" in html.lower()
    assert "/projects/proj/db-console/dep/status" in html  # self-polls while starting


def test_modal_running_state_renders_through_roos():
    session = DbConsoleSession(
        session_id="abcd1234",
        name="dbconsole-proj-dep-abcd1234",
        namespace="rig-proj",
        project="proj",
        deployment="dep",
        tool=DbConsoleTool.PGWEB,
        mode=DbConsoleMode.READ_ONLY,
        opened_by="u@x.nl",
        hostname="dbconsole-abcd1234.example.com",
        url="https://dbconsole-abcd1234.example.com/",
        expires_at=datetime(2026, 6, 26, 22, 0, tzinfo=UTC),
    )
    html = _render_modal_html(
        project_name="proj",
        deployment_name="dep",
        session=session,
        state="running",
        error=None,
        ttl_seconds=3600,
        enabled=True,
    )
    assert "Console openen" in html
    assert "Nu stoppen" in html


def test_modal_error_renders_through_roos():
    html = _render_modal_html(
        project_name="proj",
        deployment_name="dep",
        session=None,
        state="none",
        error="Boem",
        ttl_seconds=3600,
        enabled=True,
    )
    assert "Boem" in html


# ------------------------------------------------------------------ reaper TTL


def test_reaper_expiry_detection():
    now = datetime.now(UTC)
    assert DbConsoleReaper._is_expired((now - timedelta(minutes=1)).isoformat(), now) is True
    assert DbConsoleReaper._is_expired((now + timedelta(minutes=1)).isoformat(), now) is False
    assert DbConsoleReaper._is_expired(None, now) is False
    assert DbConsoleReaper._is_expired("not-a-date", now) is False


# --------------------------------------------------- read-only grant is SELECT-only


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql: str, *args) -> None:
        self.executed.append(sql)

    async def fetchval(self, sql: str, *args) -> int:
        return 1  # schema + user exist


class _FakePool:
    """Minimal DatabasePool stand-in that returns one canned row and records SQL."""

    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.calls: list[tuple[str, str, tuple]] = []

    async def acquire(self):
        pool = self

        class _Conn:
            async def fetchrow(self, sql, *args):
                pool.calls.append(("fetchrow", sql, args))
                return pool.row

            async def fetch(self, sql, *args):
                pool.calls.append(("fetch", sql, args))
                return [pool.row] if pool.row else []

            async def execute(self, sql, *args):
                pool.calls.append(("execute", sql, args))

        return _Conn()

    async def release(self, conn) -> None:
        return None


@pytest.mark.asyncio
async def test_runs_service_create_and_end():
    from opi.services.runs_service import RunKind, RunsService, RunStatus

    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "kind": "db-console",
        "session_id": "abcd1234",
        "project": "proj",
        "deployment": "dep",
        "spec": {"tool": "pgweb", "mode": "ro"},
        "status": "starting",
    }
    pool = _FakePool(row)
    svc = RunsService(pool)  # type: ignore[arg-type]

    result = await svc.create_run(
        kind=RunKind.DB_CONSOLE,
        session_id="abcd1234",
        cluster="sandboxed-local",
        project="proj",
        deployment="dep",
        namespace="rig-proj",
        name="dbconsole-proj-dep-abcd1234",
        spec={"tool": "pgweb", "mode": "ro"},
        url="https://x/",
        started_by="u@x.nl",
        expires_at=None,
    )
    assert result["session_id"] == "abcd1234"
    assert result["spec"] == {"tool": "pgweb", "mode": "ro"}
    assert any(kind == "fetchrow" and "INSERT INTO runs" in sql for kind, sql, _ in pool.calls)

    await svc.mark_ended("abcd1234", RunStatus.EXPIRED, ended_by="reaper")
    assert any(kind == "execute" and "status = $2" in sql for kind, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_grant_readonly_is_select_only(monkeypatch):
    pg = create_postgres_connector("rig-db-rw", "postgres", "AdminPw123")
    fake = _FakeConn()

    async def _fake_get(_db):
        return fake

    monkeypatch.setattr(pg, "_get_or_create_connection", _fake_get)

    result = await pg.grant_readonly_on_schema("proj_dep", "proj_dep", "proj_dep_ro_abcd1234")
    assert result["status"] == "granted"

    joined = " | ".join(fake.executed)
    assert "GRANT CONNECT ON DATABASE" in joined
    assert "GRANT USAGE ON SCHEMA" in joined
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA" in joined
    assert "ALTER DEFAULT PRIVILEGES" in joined
    # The whole point of read-only: never grant write/all.
    assert "GRANT ALL" not in joined
    assert "INSERT" not in joined


@pytest.mark.asyncio
async def test_grant_readonly_raises_when_schema_missing(monkeypatch):
    """A missing schema/user must raise (not silently produce a no-grant console)."""
    from opi.connectors.postgres import PostgresExecutionError

    pg = create_postgres_connector("rig-db-rw", "postgres", "AdminPw123")

    class _NoSchemaConn:
        async def execute(self, sql: str, *args) -> None: ...

        async def fetchval(self, sql: str, *args):
            return None  # schema / user do not exist

    async def _fake_get(_db):
        return _NoSchemaConn()

    monkeypatch.setattr(pg, "_get_or_create_connection", _fake_get)

    with pytest.raises(PostgresExecutionError):
        await pg.grant_readonly_on_schema("proj_dep", "missing_schema", "role_x")
