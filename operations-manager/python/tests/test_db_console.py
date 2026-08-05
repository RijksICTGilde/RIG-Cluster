"""Unit tests for the ephemeral database console feature."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml
from opi.core.db_console_reaper import DbConsoleReaper
from opi.core.templates import get_templates
from opi.generation.manifests import render_template
from opi.manager.db_console_manager import (
    DbConsoleManager,
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


def test_tool_config_pgweb_builds_database_url():
    _image, port, args, secret = DbConsoleManager._tool_config(DbConsoleTool.PGWEB, _db_secret(), "app_user", "app_pw")
    assert port == 8081
    assert "--lock-session" in args  # pins to the single connection; no host switching
    assert secret["DATABASE_URL"].startswith("postgresql://app_user:app_pw@rig-db-rw:5432/proj_dep")
    assert "search_path" in secret["DATABASE_URL"]


def test_tool_config_dbgate_single_connection_env():
    _image, port, _args, secret = DbConsoleManager._tool_config(
        DbConsoleTool.DBGATE, _db_secret(), "proj_dep", "secretpw1"
    )
    assert port == 3000
    assert secret["CONNECTIONS"] == "con1"
    assert secret["SERVER_con1"] == "rig-db-rw"
    assert secret["USER_con1"] == "proj_dep"
    assert secret["ENGINE_con1"] == "postgres@dbgate-plugin-postgres"
    assert secret["DISABLE_VOLATILE_CONNECTIONS"] == "1"  # no ad-hoc connections


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
    tmpl = get_templates().get_template("shared/_db-console-modal.html.j2")
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


# ------------------------------------------------------------- runs registry


@pytest.mark.asyncio
async def test_runs_service_create_and_end(orm_db):
    from opi.services.runs_service import RunKind, RunsService, RunStatus

    svc = RunsService()
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
    assert result["status"] == "starting"

    await svc.mark_ended("abcd1234", RunStatus.EXPIRED, ended_by="reaper")
    latest = await svc.get_latest_run("proj", "dep", RunKind.DB_CONSOLE)
    assert latest["status"] == "expired"
    assert latest["ended_by"] == "reaper"


@pytest.mark.asyncio
async def test_get_latest_run_queries_newest(orm_db):
    from opi.services.runs_service import RunKind, RunsService

    svc = RunsService()
    for sid, nm in (("s1", "n1"), ("s2", "n2")):
        await svc.create_run(
            kind=RunKind.DB_CONSOLE,
            session_id=sid,
            cluster="c",
            project="proj",
            deployment="dep",
            namespace="ns",
            name=nm,
            spec={},
            url=None,
            started_by=None,
            expires_at=None,
        )
    latest = await svc.get_latest_run("proj", "dep", RunKind.DB_CONSOLE)
    assert latest["session_id"] == "s2"  # newest by started_at


def test_is_stale_starting():
    """A pod-less 'starting' run older than the window is reconcilable (so begin() can
    clear it instead of wedging forever); a fresh one, a terminal one, or None is not."""
    from opi.manager.run_support import is_stale_starting

    fresh = {"status": "starting", "started_at": datetime.now(UTC).isoformat()}
    stale = {"status": "starting", "started_at": (datetime.now(UTC) - timedelta(seconds=600)).isoformat()}
    assert is_stale_starting(fresh) is False
    assert is_stale_starting(stale) is True
    assert is_stale_starting({"status": "running", "started_at": stale["started_at"]}) is False
    assert is_stale_starting(None) is False


@pytest.mark.asyncio
async def test_pending_state_maps_registry(monkeypatch):
    """The status poll derives starting/failed/stale/none from the runs registry.

    pending_state is the shared run_support helper used by both the db-console and
    job routers; here it is exercised for the db-console kind.
    """
    from opi.manager import run_support as r
    from opi.services.runs_service import RunKind

    class _FakeRuns:
        def __init__(self, run):
            self._run = run

        async def get_latest_run(self, *a, **k):
            return self._run

    async def call():
        return await r.pending_state("p", "d", RunKind.DB_CONSOLE, "console", None)

    # in-flight start -> 'starting'
    monkeypatch.setattr(
        r, "get_runs_service", lambda: _FakeRuns({"status": "starting", "started_at": datetime.now(UTC).isoformat()})
    )
    assert (await call())[0] == "starting"

    # failed start -> form + the recorded error
    monkeypatch.setattr(r, "get_runs_service", lambda: _FakeRuns({"status": "failed", "error_message": "boom"}))
    assert await call() == ("none", "boom")

    # no run -> 'none'
    monkeypatch.setattr(r, "get_runs_service", lambda: _FakeRuns(None))
    assert (await call())[0] == "none"

    # stale 'starting' (provisioning died) -> none + time-out message
    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    monkeypatch.setattr(r, "get_runs_service", lambda: _FakeRuns({"status": "starting", "started_at": old}))
    state, err = await call()
    assert state == "none"
    assert err is not None
    assert "time-out" in err
