"""
Ephemeral database console manager.

On request, spins up a temporary web database client (pgweb or dbgate) for a
single deployment's PostgreSQL database, fronted by the existing oauth2-proxy
"auth wall" so only the project's members (by email) can reach it. The console
auto-expires; OPI applies and removes it directly via kubectl, outside
git/ArgoCD. See features/futures/fsc-mtls-attachments.md sibling design notes.

Design highlights:
- Reuses the production service/ingress/auth-wall templates; only the workload
  is a small ephemeral Pod (kind: Pod) with activeDeadlineSeconds as a hard
  self-terminate backstop.
- Authenticates against the ZAD realm (derived from OIDC_DISCOVERY_URL) with a
  dedicated per-session OIDC client created on demand and deleted at teardown -
  no Keycloak redirect-URI wildcards, no shared client.
- Read-only mode provisions an ephemeral SELECT-only PostgreSQL role (the tool's
  own read-only flag is only a hint); read-write reuses the deployment's DB user.
- The console Pod carries the target deployment's `deployment` label so the
  existing tenant NetworkPolicy already permits egress to PostgreSQL.
- TLS relies on the cluster's default/wildcard ingress certificate (no per-host
  cert issuance), so ephemeral hostnames do not churn Let's Encrypt.
"""

from __future__ import annotations

import base64
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import create_kubectl_connector
from opi.connectors.postgres import create_postgres_connector
from opi.core.cluster_config import (
    get_infrastructure_namespace,
    get_ingress_ip_whitelist,
    get_ingress_postfix,
    get_namespace_prefix,
)
from opi.core.config import settings
from opi.generation.manifests import render_template
from opi.manager.run_support import (
    ANNOT_EXPIRES,
    ANNOT_HOSTNAME,
    ANNOT_OPENED_BY,
    LABEL_RUN,
    LABEL_RUN_DEPLOYMENT,
    LABEL_RUN_KIND,
    LABEL_RUN_PROJECT,
    apply_bundle,
    delete_bundle,
    find_deployment,
    parse_expires,
    resolve_image,
)
from opi.services.project_service import get_project_service
from opi.services.runs_service import RunKind, RunStatus, get_runs_service
from opi.services.services_enums import ServiceType
from opi.utils.naming import (
    generate_db_console_client_id,
    generate_db_console_hostname,
    generate_db_console_name,
    generate_db_console_ro_role,
)
from opi.utils.secrets import DatabaseSecret

logger = logging.getLogger(__name__)

# Shared run labels (LABEL_RUN/_PROJECT/_DEPLOYMENT) and the expires/opened-by/
# hostname annotations come from run_support. These aliases keep the console code
# readable while the reaper discovers every kind by the same LABEL_RUN.
LABEL_SESSION = LABEL_RUN
LABEL_PROJECT = LABEL_RUN_PROJECT
LABEL_DEPLOYMENT = LABEL_RUN_DEPLOYMENT

# Console-specific annotations (carry everything teardown needs after a restart).
ANNOT_MODE = "rig.zad/db-console-mode"
ANNOT_TOOL = "rig.zad/db-console-tool"
ANNOT_REALM = "rig.zad/oidc-realm"
ANNOT_CLIENT_ID = "rig.zad/oidc-client-id"
ANNOT_RO_ROLE = "rig.zad/ephemeral-role"
ANNOT_DB_HOST = "rig.zad/db-host"
ANNOT_DB_NAME = "rig.zad/db-name"


class DbConsoleTool(StrEnum):
    """Selectable web database client."""

    PGWEB = "pgweb"
    DBGATE = "dbgate"


class DbConsoleMode(StrEnum):
    """Access mode for a console session."""

    READ_ONLY = "ro"
    READ_WRITE = "rw"


# The oauth2-proxy sidecar (Service targetPort) port; the tool listens behind it.
AUTH_WALL_PORT = 4180
_TOOL_PORTS = {DbConsoleTool.PGWEB: 8081, DbConsoleTool.DBGATE: 3000}


# Client ids created very recently, to protect them from the orphan-client GC
# during the start race (the OIDC client is created before its pod is visible).
# Intra-process only (manager + reaper share this process); a restart clears it,
# by which point any client is either pod-backed or genuinely orphaned.
_recent_client_ids: dict[str, float] = {}


def mark_client_created(client_id: str) -> None:
    """Record that an OIDC client was just created (orphan-GC grace)."""
    _recent_client_ids[client_id] = time.monotonic()


def client_recently_created(client_id: str, grace_seconds: float) -> bool:
    """True if client_id was created within grace_seconds; prunes stale entries."""
    now = time.monotonic()
    for cid, ts in list(_recent_client_ids.items()):
        if now - ts > grace_seconds:
            del _recent_client_ids[cid]
    return client_id in _recent_client_ids


def _enum_or_default[E: StrEnum](enum_cls: type[E], value: str | None, default: E) -> E:
    """Parse a stored annotation string into an enum, falling back on bad/missing data."""
    try:
        return enum_cls(value) if value else default
    except ValueError:
        return default


class DbConsoleError(Exception):
    """Raised when a database console cannot be started or torn down."""


@dataclass
class DbConsoleSession:
    """Public description of a live (or just-created) console session."""

    session_id: str
    name: str
    namespace: str
    project: str
    deployment: str
    tool: DbConsoleTool
    mode: DbConsoleMode
    opened_by: str
    hostname: str
    url: str
    expires_at: datetime


class DbConsoleManager:
    """Orchestrates the lifecycle of ephemeral database consoles."""

    def __init__(self) -> None:
        self._kubectl = create_kubectl_connector()
        self._project_service = get_project_service()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _zad_realm() -> str:
        """Derive the ZAD realm from the platform OIDC discovery URL."""
        discovery_url = settings.OIDC_DISCOVERY_URL
        if not discovery_url or "/realms/" not in discovery_url:
            raise DbConsoleError(
                "Platform OIDC is not configured (OIDC_DISCOVERY_URL missing); cannot start a database console."
            )
        return discovery_url.split("/realms/", 1)[1].split("/", 1)[0]

    @staticmethod
    def _issuer_url() -> str:
        """oauth2-proxy issuer = the ZAD issuer (discovery URL without the suffix)."""
        return (settings.OIDC_DISCOVERY_URL or "").replace("/.well-known/openid-configuration", "")

    @staticmethod
    def _uses_namespace_postgresql(project_data: dict[str, Any]) -> bool:
        """True if the project uses a namespace-dedicated PostgreSQL (vs the shared server).

        Mirrors DatabaseManager._project_uses_namespace_postgresql: the choice is
        driven by the project-level `services` config, not by the DB hostname.
        """
        for service_item in project_data.get("services", []) or []:
            if isinstance(service_item, str) and service_item == ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value:
                return True
            if isinstance(service_item, dict) and ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in service_item:
                return True
        return False

    async def _admin_connector(self, db_host: str, project_name: str, cluster: str):
        """Build a PostgreSQL admin connector bound to the deployment's DB server.

        Shared server -> platform admin credentials; a namespace-dedicated server
        -> the CloudNativePG superuser secret in the infra namespace. Whether the
        project is namespace-dedicated is read from its service config (not from
        the hostname), matching how the database manager resolves credentials.
        """
        project = self._project_service.get_project(project_name)
        uses_namespace = bool(project and project.data and self._uses_namespace_postgresql(project.data))

        if not uses_namespace:
            return create_postgres_connector(
                host=db_host,
                admin_username=settings.DATABASE_ADMIN_NAME,
                admin_password=settings.DATABASE_ADMIN_PASSWORD,
            )

        infra_namespace = get_infrastructure_namespace(cluster, project_name)
        secret_name = f"{project_name.lower()}-postgres-superuser"
        secret_data = await self._kubectl.get_secret(secret_name, infra_namespace)
        if not secret_data or not secret_data.get("username") or not secret_data.get("password"):
            raise DbConsoleError(
                f"Superuser secret '{secret_name}' not found in '{infra_namespace}'; cannot provision read-only role."
            )
        return create_postgres_connector(
            host=db_host,
            admin_username=secret_data["username"],
            admin_password=secret_data["password"],
        )

    @staticmethod
    def _tool_config(
        tool: DbConsoleTool, mode: DbConsoleMode, db_secret: DatabaseSecret, user: str, password: str
    ) -> tuple[str, int, list[str], dict[str, str]]:
        """Return (image, container_port, args, connection_secret_data) per tool."""
        port = _TOOL_PORTS[tool]
        read_only = mode == DbConsoleMode.READ_ONLY
        if tool == DbConsoleTool.PGWEB:
            image = settings.DB_CONSOLE_PGWEB_IMAGE
            # --lock-session pins pgweb to the single configured connection and hides
            # the disconnect / "new connection" UI, so a logged-in member cannot point
            # the console at another host/database. --no-ssh blocks SSH tunnels too.
            args = ["--bind=0.0.0.0", f"--listen={port}", "--no-ssh", "--lock-session"]
            if read_only:
                args.append("--readonly")
            url = (
                f"postgresql://{user}:{password}@{db_secret.host}:{db_secret.port}/{db_secret.database}"
                f"?options=--search_path%3D{db_secret.schema},public"
            )
            return image, port, args, {"DATABASE_URL": url}

        # dbgate
        image = settings.DB_CONSOLE_DBGATE_IMAGE
        conn: dict[str, str] = {
            # Lock dbgate to the single predefined connection: no ad-hoc/volatile
            # connections, so a member cannot connect the console to another host.
            "DISABLE_VOLATILE_CONNECTIONS": "1",
            "CONNECTIONS": "con1",
            "LABEL_con1": db_secret.database,
            "SERVER_con1": db_secret.host,
            "PORT_con1": str(db_secret.port),
            "USER_con1": user,
            "PASSWORD_con1": password,
            "DATABASE_con1": db_secret.database,
            "ENGINE_con1": "postgres@dbgate-plugin-postgres",
        }
        if read_only:
            conn["READONLY_con1"] = "1"
        return image, port, [], conn

    # ------------------------------------------------------------------- public

    async def start(
        self,
        project_name: str,
        deployment_name: str,
        mode: DbConsoleMode,
        tool: DbConsoleTool,
        opened_by: str,
    ) -> DbConsoleSession:
        """Provision and apply a console bundle; return the session description."""
        project = self._project_service.get_project(project_name)
        if not project or not project.data:
            raise DbConsoleError(f"Project '{project_name}' not found")

        deployment = find_deployment(project.data, deployment_name)
        if not deployment:
            raise DbConsoleError(f"Deployment '{deployment_name}' not found in project '{project_name}'")

        cluster = deployment.get("cluster", settings.CLUSTER_MANAGER)
        if cluster != settings.CLUSTER_MANAGER:
            raise DbConsoleError(
                f"Deployment '{deployment_name}' runs on cluster '{cluster}', not managed by this instance."
            )

        namespace = f"{get_namespace_prefix(cluster)}{project_name}"

        db_secret = await DatabaseSecret.get_data(
            kubectl_connector=self._kubectl, namespace=namespace, prefix=deployment_name
        )
        if not db_secret:
            raise DbConsoleError(f"Deployment '{deployment_name}' has no database to connect to.")

        # One active console per deployment.
        existing = await self._kubectl.get_resources_by_label("pod", namespace, f"{LABEL_DEPLOYMENT}={deployment_name}")
        if existing:
            raise DbConsoleError(
                f"A database console is already running for deployment '{deployment_name}'. "
                "Stop it before starting a new one."
            )

        session_id = secrets.token_hex(4)
        name = generate_db_console_name(project_name, deployment_name, session_id)
        hostname = generate_db_console_hostname(project_name, session_id, get_ingress_postfix(cluster))
        client_id = generate_db_console_client_id(session_id)
        realm = self._zad_realm()

        # Dedicated per-session OIDC client (deleted at teardown).
        keycloak = await create_keycloak_connector()
        callback = f"https://{hostname}/oauth2/callback"
        client = await keycloak.create_oidc_client(
            realm_name=realm,
            client_id=client_id,
            client_name=f"DB console {project_name}/{deployment_name}",
            redirect_uris=[callback, f"https://{hostname}/*"],
            web_origins=[f"https://{hostname}"],
        )
        client_secret = client.get("secret") or client.get("client_secret")
        if not client_secret:
            raise DbConsoleError("Failed to obtain OIDC client secret for the database console.")
        # Protect this fresh client from the reaper's orphan GC until its pod is visible.
        mark_client_created(client_id)

        # Connection credentials: RW reuses the app user; RO provisions a role.
        ro_role: str | None = None
        if mode == DbConsoleMode.READ_ONLY:
            ro_role = generate_db_console_ro_role(project_name, deployment_name, session_id)
            ro_password = f"Db{secrets.token_hex(20)}9"
            pg = await self._admin_connector(db_secret.host, project_name, cluster)
            try:
                await pg.create_user(ro_role, ro_password)
                await pg.grant_readonly_on_schema(db_secret.database, db_secret.schema, ro_role)
                await pg.set_role_search_path(ro_role, db_secret.database, db_secret.schema)
            finally:
                await pg.close()
            conn_user, conn_password = ro_role, ro_password
        else:
            conn_user, conn_password = db_secret.username, db_secret.password

        tool_image, application_port, tool_args, conn_secret = self._tool_config(
            tool, mode, db_secret, conn_user, conn_password
        )
        # Shared local/ image handling (Kind-loaded tool images -> pull policy Never).
        tool_image, image_pull_policy = resolve_image(tool_image)

        cookie_secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        secret_data = {**conn_secret, "OIDC_CLIENT_SECRET": client_secret, "cookie-secret": cookie_secret}

        expires_at = datetime.now(UTC) + timedelta(seconds=settings.DB_CONSOLE_TTL_SECONDS)
        extra_labels = {
            LABEL_SESSION: session_id,
            LABEL_RUN_KIND: str(RunKind.DB_CONSOLE),
            LABEL_PROJECT: project_name,
            LABEL_DEPLOYMENT: deployment_name,
        }
        extra_annotations = {
            ANNOT_EXPIRES: expires_at.isoformat(),
            ANNOT_MODE: mode.value,
            ANNOT_TOOL: tool.value,
            ANNOT_OPENED_BY: opened_by,
            ANNOT_HOSTNAME: hostname,
            ANNOT_REALM: realm,
            ANNOT_CLIENT_ID: client_id,
            ANNOT_DB_HOST: db_secret.host,
            ANNOT_DB_NAME: db_secret.database,
        }
        if ro_role:
            extra_annotations[ANNOT_RO_ROLE] = ro_role

        authorization_wall = {
            "issuer_url": self._issuer_url(),
            "client_id": client_id,
            "keycloak_secret_name": name,
            "cookie_secret_name": name,
            "authenticated_emails_configmap": f"{name}-emails",
            "banner": "Tijdelijke databaseconsole",
        }

        emails = sorted({u.email.lower() for u in (project.users or []) if u.email})

        common = {
            "name": name,
            "namespace": namespace,
            "project": {"name": project_name},
            "cluster": cluster,
            "extra_labels": extra_labels,
            "extra_annotations": extra_annotations,
        }

        try:
            await self._apply_bundle(
                namespace=namespace,
                common=common,
                secret_data=secret_data,
                emails=emails,
                authorization_wall=authorization_wall,
                target_deployment=deployment_name,
                tool_image=tool_image,
                image_pull_policy=image_pull_policy,
                application_port=application_port,
                tool_args=tool_args,
                hostname=hostname,
                cluster=cluster,
            )
        except Exception:
            # Roll back a partial start so nothing is left behind: the label-scoped
            # delete clears any resources that did apply, plus the OIDC client and
            # the ephemeral read-only role we created above.
            await self.teardown(
                namespace=namespace,
                session_id=session_id,
                project_name=project_name,
                cluster=cluster,
                realm=realm,
                client_id=client_id,
                ro_role=ro_role,
                db_host=db_secret.host,
                end_status=RunStatus.FAILED,
            )
            raise

        # Register the run for administration/history. Best-effort: the cluster
        # bundle is the live source of truth, so a DB hiccup must not fail the start
        # (the label-based reaper still cleans up even without a row).
        try:
            await get_runs_service().create_run(
                kind=RunKind.DB_CONSOLE,
                session_id=session_id,
                cluster=cluster,
                project=project_name,
                deployment=deployment_name,
                namespace=namespace,
                name=name,
                spec={"tool": tool.value, "mode": mode.value},
                url=f"https://{hostname}/",
                started_by=opened_by,
                expires_at=expires_at,
            )
        except Exception:
            logger.exception("Failed to record run for database console session %s (continuing)", session_id)

        logger.info(
            f"Started {tool} {mode} database console '{name}' for {project_name}/{deployment_name} "
            f"by {opened_by}, expires {expires_at.isoformat()}"
        )

        return DbConsoleSession(
            session_id=session_id,
            name=name,
            namespace=namespace,
            project=project_name,
            deployment=deployment_name,
            tool=tool,
            mode=mode,
            opened_by=opened_by,
            hostname=hostname,
            url=f"https://{hostname}/",
            expires_at=expires_at,
        )

    async def _apply_bundle(
        self,
        *,
        namespace: str,
        common: dict[str, Any],
        secret_data: dict[str, str],
        emails: list[str],
        authorization_wall: dict[str, Any],
        target_deployment: str,
        tool_image: str,
        image_pull_policy: str,
        application_port: int,
        tool_args: list[str],
        hostname: str,
        cluster: str,
    ) -> None:
        """Render every manifest and apply it directly via kubectl."""
        manifests: dict[str, str] = {}
        manifests["secret"] = render_template("db-console-secret.yaml.jinja", {**common, "secret_data": secret_data})
        manifests["emails"] = render_template("db-console-emails-configmap.yaml.jinja", {**common, "emails": emails})
        manifests["signin"] = render_template(
            "sidecar-authorization-wall.yaml.jinja",
            {**common, "section": "configmap", "authorization_wall": authorization_wall},
        )
        manifests["service"] = render_template(
            "service.yaml.jinja", {**common, "service_port": AUTH_WALL_PORT, "application_port": application_port}
        )
        manifests["pod"] = render_template(
            "db-console-pod.yaml.jinja",
            {
                **common,
                "target_deployment": target_deployment,
                "ttl_seconds": settings.DB_CONSOLE_TTL_SECONDS,
                "tool_image": tool_image,
                "image_pull_policy": image_pull_policy,
                "application_port": application_port,
                "tool_args": tool_args,
                "secret_name": common["name"],
                "hostname": hostname,
                "authorization_wall": authorization_wall,
            },
        )
        manifests["ingress"] = render_template(
            "ingress.yaml.jinja",
            {
                **common,
                "hostname": hostname,
                "service_port": AUTH_WALL_PORT,
                "enable_tls": True,
                "ip_whitelist": get_ingress_ip_whitelist(cluster),
                "path": "/",
            },
        )

        # One-time action: apply the whole bundle as a single multi-document
        # manifest. Every resource carries the rig.zad/run=<session> label, so
        # teardown removes the bundle with a single label-scoped delete.
        order = ["secret", "emails", "signin", "service", "pod", "ingress"]
        ok, stderr = await apply_bundle(self._kubectl, namespace, [manifests[key] for key in order], cluster)
        if not ok:
            raise DbConsoleError(f"Kon de databaseconsole niet aanmaken: {stderr.strip() or 'onbekende fout'}")

    async def teardown(
        self,
        *,
        namespace: str,
        session_id: str,
        project_name: str,
        cluster: str,
        realm: str | None,
        client_id: str | None,
        ro_role: str | None,
        db_host: str | None,
        end_status: RunStatus = RunStatus.STOPPED,
        ended_by: str | None = None,
    ) -> None:
        """Remove an entire console bundle in one action (by shared label). Idempotent."""
        # Label-scoped delete removes Pod, Service, Ingress, Secret and both ConfigMaps.
        await delete_bundle(self._kubectl, namespace, session_id)

        # Keycloak client.
        if realm and client_id:
            try:
                keycloak = await create_keycloak_connector()
                await keycloak.delete_oidc_client(realm, client_id)
            except Exception:
                logger.exception(f"Failed to delete OIDC client '{client_id}' for console session '{session_id}'")

        # Ephemeral read-only role.
        if ro_role and db_host:
            try:
                pg = await self._admin_connector(db_host, project_name, cluster)
                try:
                    await pg.delete_user(ro_role)
                finally:
                    await pg.close()
            except Exception:
                logger.exception(f"Failed to drop ephemeral role '{ro_role}' for console session '{session_id}'")

        # Record the terminal state for administration/history (best-effort).
        try:
            await get_runs_service().mark_ended(session_id, end_status, ended_by=ended_by)
        except Exception:
            logger.exception(f"Failed to mark run ended for console session '{session_id}'")

        logger.info(f"Tore down database console session '{session_id}' in namespace '{namespace}'")

    async def teardown_session(self, project_name: str, session_id: str, ended_by: str | None = None) -> str | None:
        """Tear down a console by session id (reads teardown info from the pod).

        Returns the deployment name, or None if no matching console was found.
        Used by the manual "stop" action, so the run is recorded as stopped.
        """
        cluster = settings.CLUSTER_MANAGER
        namespace = f"{get_namespace_prefix(cluster)}{project_name}"
        pods = await self._kubectl.get_resources_by_label("pod", namespace, f"{LABEL_SESSION}={session_id}")
        if not pods:
            return None
        meta = pods[0].get("metadata", {})
        labels = meta.get("labels", {}) or {}
        annotations = meta.get("annotations", {}) or {}
        await self.teardown(
            namespace=namespace,
            session_id=session_id,
            project_name=project_name,
            cluster=cluster,
            realm=annotations.get(ANNOT_REALM),
            client_id=annotations.get(ANNOT_CLIENT_ID),
            ro_role=annotations.get(ANNOT_RO_ROLE),
            db_host=annotations.get(ANNOT_DB_HOST),
            end_status=RunStatus.STOPPED,
            ended_by=ended_by,
        )
        return labels.get(LABEL_DEPLOYMENT)

    async def get_console_status(self, project_name: str, deployment_name: str) -> tuple[DbConsoleSession | None, bool]:
        """Return the live console for a deployment and whether its pod is Ready.

        (session, ready): session is None when no console exists; ready is True
        only when the pod is Running with all containers (tool + auth wall) ready.
        """
        cluster = settings.CLUSTER_MANAGER
        namespace = f"{get_namespace_prefix(cluster)}{project_name}"
        pods = await self._kubectl.get_resources_by_label("pod", namespace, f"{LABEL_DEPLOYMENT}={deployment_name}")
        if not pods:
            return None, False
        pod = pods[0]
        session = self._session_from_pod(pod, namespace, project_name)
        status = pod.get("status", {}) or {}
        container_statuses = status.get("containerStatuses", []) or []
        ready = (
            status.get("phase") == "Running"
            and bool(container_statuses)
            and all(c.get("ready") for c in container_statuses)
        )
        return session, ready

    async def list_sessions(self, project_name: str) -> list[DbConsoleSession]:
        """List live console sessions for a project (read from cluster labels)."""
        cluster = settings.CLUSTER_MANAGER
        namespace = f"{get_namespace_prefix(cluster)}{project_name}"
        pods = await self._kubectl.get_resources_by_label("pod", namespace, f"{LABEL_PROJECT}={project_name}")
        return [self._session_from_pod(pod, namespace, project_name) for pod in pods]

    @staticmethod
    def _session_from_pod(pod: dict[str, Any], namespace: str, project_name: str) -> DbConsoleSession:
        meta = pod.get("metadata", {})
        labels = meta.get("labels", {}) or {}
        annotations = meta.get("annotations", {}) or {}
        expires_at = parse_expires(annotations.get(ANNOT_EXPIRES)) or datetime.now(UTC)
        hostname = annotations.get(ANNOT_HOSTNAME, "")
        tool = _enum_or_default(DbConsoleTool, annotations.get(ANNOT_TOOL), DbConsoleTool.PGWEB)
        mode = _enum_or_default(DbConsoleMode, annotations.get(ANNOT_MODE), DbConsoleMode.READ_ONLY)
        return DbConsoleSession(
            session_id=labels.get(LABEL_SESSION, ""),
            name=meta.get("name", ""),
            namespace=namespace,
            project=project_name,
            deployment=labels.get(LABEL_DEPLOYMENT, ""),
            tool=tool,
            mode=mode,
            opened_by=annotations.get(ANNOT_OPENED_BY, ""),
            hostname=hostname,
            url=f"https://{hostname}/" if hostname else "",
            expires_at=expires_at,
        )


_db_console_manager: DbConsoleManager | None = None


def get_db_console_manager() -> DbConsoleManager:
    """Return the process-wide DbConsoleManager."""
    global _db_console_manager
    if _db_console_manager is None:
        _db_console_manager = DbConsoleManager()
    return _db_console_manager
