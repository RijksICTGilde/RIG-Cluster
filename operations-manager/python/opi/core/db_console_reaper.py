"""Reaper for ephemeral database consoles.

Every interval this lists the database-console pods on this cluster, tears down
any whose TTL has expired, and garbage-collects orphaned Keycloak clients whose
pod is gone. State lives entirely in cluster labels/annotations (and Postgres),
so the reaper is restart-safe: after an OPI restart it rediscovers and cleans up
everything by label. The pod's own activeDeadlineSeconds is the hard backstop if
the reaper is ever down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import get_namespace_prefix
from opi.core.config import settings
from opi.manager.db_console_manager import (
    ANNOT_CLIENT_ID,
    ANNOT_REALM,
    DbConsoleManager,
    client_recently_created,
    get_db_console_manager,
)
from opi.manager.run_support import ANNOT_EXPIRES, LABEL_RUN
from opi.services.project_service import get_project_service
from opi.services.runs_service import RunStatus
from opi.utils.naming import DB_CONSOLE_PREFIX

logger = logging.getLogger(__name__)

# A bundle is applied within seconds, so a pod-less bundle older than this is a
# failed/partial apply, not one mid-creation. Generous to avoid any race.
ORPHAN_GRACE_SECONDS = 300


class DbConsoleReaper:
    """Tears down expired database consoles and orphaned OIDC clients."""

    def __init__(self, cluster: str):
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Database console reaper started (cluster=%s, every %ds)",
            self._cluster,
            settings.DB_CONSOLE_REAP_INTERVAL_SECONDS,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Database console reaper stopped")

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(settings.DB_CONSOLE_REAP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            try:
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in database console reaper sweep")

    async def _sweep(self) -> None:
        kubectl = create_kubectl_connector()
        manager = get_db_console_manager()
        now = datetime.now(UTC)

        live_client_ids: set[str] = set()
        projects = get_project_service().get_all_projects()
        for project in projects.values():
            if not project.data:
                continue
            name = project.data.get("name", "")
            if not name:
                continue
            if not any(d.get("cluster") == self._cluster for d in project.data.get("deployments", []) or []):
                continue

            namespace = f"{get_namespace_prefix(self._cluster)}{name}"
            pods = await kubectl.get_resources_by_label("pod", namespace, LABEL_RUN)
            live_sessions: set[str] = set()
            for pod in pods:
                meta = pod.get("metadata", {})
                labels = meta.get("labels", {}) or {}
                annotations = meta.get("annotations", {}) or {}
                session_id = labels.get(LABEL_RUN, "")
                client_id = annotations.get(ANNOT_CLIENT_ID)
                if client_id:
                    live_client_ids.add(client_id)
                if self._is_expired(annotations.get(ANNOT_EXPIRES), now):
                    await self._teardown_pod(manager, meta, namespace)
                elif session_id:
                    live_sessions.add(session_id)

            await self._reap_orphans(kubectl, manager, namespace, live_sessions, now)

        await self._gc_orphan_clients(live_client_ids)

    async def _reap_orphans(
        self,
        kubectl,
        manager: DbConsoleManager,
        namespace: str,
        live_sessions: set[str],
        now: datetime,
    ) -> None:
        """Remove pod-less bundle leftovers (e.g. a half-applied bundle whose Service
        was rejected before the Pod). The reaper can only see expiry via the Pod, so
        a bundle that never got a Pod would otherwise linger forever. Found purely by
        the rig.zad/db-console label; only swept once older than the grace window so a
        bundle mid-creation is never touched.
        """
        items = await kubectl.get_resources_by_label("secret,configmap,service,ingress", namespace, LABEL_RUN)
        oldest_by_session: dict[str, datetime] = {}
        for item in items:
            meta = item.get("metadata", {})
            session_id = (meta.get("labels", {}) or {}).get(LABEL_RUN, "")
            if not session_id or session_id in live_sessions:
                continue
            created = self._parse_ts(meta.get("creationTimestamp"))
            if created is None:
                continue
            if session_id not in oldest_by_session or created < oldest_by_session[session_id]:
                oldest_by_session[session_id] = created

        for session_id, created in oldest_by_session.items():
            if (now - created).total_seconds() < ORPHAN_GRACE_SECONDS:
                continue  # may still be mid-creation; leave it
            logger.info("Reaping orphaned database console bundle (session=%s, no pod)", session_id)
            try:
                # Pod-less, so no annotations to drive Keycloak/role cleanup; the
                # label-scoped delete removes the leftover k8s resources, and the
                # orphan-client GC below handles any stranded OIDC client.
                await manager.teardown(
                    namespace=namespace,
                    session_id=session_id,
                    realm=None,
                    client_id=None,
                    end_status=RunStatus.EXPIRED,
                )
            except Exception:
                logger.exception("Failed to reap orphaned bundle (session=%s)", session_id)

    @staticmethod
    def _parse_ts(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _is_expired(expires_raw: str | None, now: datetime) -> bool:
        if not expires_raw:
            return False
        try:
            return datetime.fromisoformat(expires_raw) <= now
        except ValueError:
            return False

    async def _teardown_pod(self, manager: DbConsoleManager, meta: dict, namespace: str) -> None:
        labels = meta.get("labels", {}) or {}
        annotations = meta.get("annotations", {}) or {}
        session_id = labels.get(LABEL_RUN, "")
        logger.info("Reaping expired database console (session=%s)", session_id)
        try:
            await manager.teardown(
                namespace=namespace,
                session_id=session_id,
                realm=annotations.get(ANNOT_REALM),
                client_id=annotations.get(ANNOT_CLIENT_ID),
                end_status=RunStatus.EXPIRED,
            )
        except Exception:
            logger.exception("Failed to reap database console (session=%s)", session_id)

    async def _gc_orphan_clients(self, live_client_ids: set[str]) -> None:
        """Delete dbconsole-* OIDC clients with no live pod (e.g. force-deleted pods)."""
        try:
            realm = DbConsoleManager._zad_realm()
        except Exception:
            return
        try:
            keycloak = await create_keycloak_connector()
            client_ids = await keycloak.list_client_ids_by_prefix(realm, f"{DB_CONSOLE_PREFIX}-")
            for client_id in client_ids:
                if client_id in live_client_ids:
                    continue
                # Skip clients created within the grace window: a console that just
                # started has its OIDC client before its pod is visible.
                if client_recently_created(client_id, ORPHAN_GRACE_SECONDS):
                    continue
                logger.info("Garbage-collecting orphaned OIDC client '%s'", client_id)
                await keycloak.delete_oidc_client(realm, client_id)
        except Exception:
            logger.exception("Failed to garbage-collect orphaned database console OIDC clients")
