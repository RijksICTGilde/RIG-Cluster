"""Report-first sweep for orphaned service resources.

Inventories actual service resources (PostgreSQL databases/users, Keycloak
clients, MinIO buckets) and classifies them against the expected set derived
from all project YAML files - the same truth source the delete API uses.

This sweep NEVER mutates anything. It produces a report; deletion happens
only via the confirm endpoint, which marks confirmed orphans for the normal
grace-period purge in ``opi.jobs.reconciliation``.

Classifications:
- ``expected``        - resource belongs to a live deployment
- ``system``          - platform infrastructure (system databases, built-in
                        Keycloak clients, backup buckets); never touched
- ``orphan_candidate``- matches our naming for a project resource, but no
                        live deployment claims it; eligible for confirmation
- ``in_use_anomaly``  - looks orphaned but shows signs of active use
                        (e.g. open database connections); NEVER confirmable,
                        needs human investigation (the waggl-9et scenario)
- ``unknown``         - does not match our naming conventions; never touched
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.minio_mc import create_minio_connector
from opi.connectors.postgres import create_postgres_connector
from opi.core.config import settings
from opi.jobs.reconciliation import _build_expected_resources
from opi.services.marked_for_deletion_service import MarkedForDeletionService
from opi.utils.naming import (
    _sanitize_for_identifier,
    _sanitize_for_lowercase,
    generate_keycloak_client_id,
    generate_project_platform_client_id,
    generate_project_realm_name,
)

if TYPE_CHECKING:
    from opi.core.database_pool import DatabasePool

logger = logging.getLogger(__name__)

SYSTEM_DATABASES = {"postgres", "template0", "template1", "keycloak", "operations_manager", "forgejo"}
BUILTIN_KEYCLOAK_CLIENTS = {
    "account",
    "account-console",
    "admin-cli",
    "broker",
    "realm-management",
    "security-admin-console",
}
SYSTEM_KEYCLOAK_CLIENTS = {"operations-manager-invites"}
SKIPPED_REALMS = {"master"}
# Platform-owned realms that exist without a project file (e.g. OPI's own SSO
# realm). Classified as system; their clients are never enumerated.
SYSTEM_REALMS = {"operations-manager"}

# Classifications that may be confirmed for deletion. Everything else is
# untouchable by design.
CONFIRMABLE = "orphan_candidate"


def _live_deployments(project_yamls: list[dict[str, Any]], cluster: str) -> dict[str, list[str]]:
    """Map project name -> deployment names on the given cluster."""
    result: dict[str, list[str]] = {}
    for project in project_yamls:
        name = project.get("name", "")
        deployments = [
            d.get("name", "") for d in project.get("deployments", []) if d.get("cluster") == cluster and d.get("name")
        ]
        if name:
            result[name] = deployments
    return result


def _classify_database(
    db_name: str,
    cluster: str,
    expected: dict[str, set[tuple[str, str]]],
    projects: dict[str, list[str]],
) -> tuple[str, str]:
    """Classify a database name. Returns (classification, reason)."""
    if db_name in SYSTEM_DATABASES or db_name.startswith("cnpg_"):
        return "system", "platform database"
    if (db_name, cluster) in expected["postgresql_database"]:
        return "expected", "belongs to a live deployment"

    for project_name in projects:
        prefix = _sanitize_for_identifier(project_name)
        if db_name == prefix or db_name.startswith(f"{prefix}_"):
            return (
                "orphan_candidate",
                f"matches project '{project_name}' naming but no live deployment claims it",
            )
    return "unknown", "does not match any project naming convention"


def _classify_project_realm_client(
    client_id: str,
    project_name: str,
    live_deployment_names: list[str],
) -> tuple[str, str]:
    """Classify a client inside a project realm."""
    if client_id in BUILTIN_KEYCLOAK_CLIENTS:
        return "system", "Keycloak built-in client"
    if client_id in SYSTEM_KEYCLOAK_CLIENTS:
        return "system", "platform-managed client"

    expected_ids: set[str] = set()
    for dep in live_deployment_names:
        base = generate_keycloak_client_id(project_name, dep)
        expected_ids.add(base)
        expected_ids.add(f"{base}-public")
    if client_id in expected_ids:
        return "expected", "belongs to a live deployment"

    project_prefix = _sanitize_for_lowercase(project_name)
    if client_id.startswith(f"{project_prefix}-"):
        return (
            "orphan_candidate",
            f"matches project '{project_name}' naming but no live deployment claims it",
        )
    return "unknown", "does not match project deployment naming"


async def sweep(
    pool: DatabasePool,
    project_yamls: list[dict[str, Any]],
    cluster: str,
    keycloak_url: str | None = None,
) -> dict[str, Any]:
    """Run the read-only orphan sweep and return a structured report.

    Each inventory section is independent: a connector failure degrades the
    report (recorded in ``errors``) instead of failing the whole sweep.
    """
    expected = _build_expected_resources(project_yamls)
    projects = _live_deployments(project_yamls, cluster)

    report: dict[str, Any] = {
        "cluster": cluster,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "databases": [],
        "keycloak_realms": [],
        "keycloak_clients": [],
        "minio_buckets": [],
        "stale_marks": [],
        "errors": [],
    }

    actual_databases: set[str] = set()

    # --- PostgreSQL databases ---
    try:
        postgres = create_postgres_connector(
            host=settings.DATABASE_HOST,
            admin_username=settings.DATABASE_ADMIN_NAME,
            admin_password=settings.DATABASE_ADMIN_PASSWORD,
        )
        for db in await postgres.list_databases():
            db_name = db.get("name") or db.get("datname", "")
            if not db_name:
                continue
            actual_databases.add(db_name)
            classification, reason = _classify_database(db_name, cluster, expected, projects)
            entry: dict[str, Any] = {"name": db_name, "classification": classification, "reason": reason}
            if classification == CONFIRMABLE:
                # Liveness gate: an orphan candidate with open connections is
                # an anomaly, not a candidate.
                active = await postgres.count_active_connections(db_name)
                if active > 0:
                    entry["classification"] = "in_use_anomaly"
                    entry["reason"] = f"{reason}; HAS {active} ACTIVE CONNECTION(S) - investigate, do not delete"
                entry["active_connections"] = active
            report["databases"].append(entry)
    except Exception as e:
        logger.exception("Orphan sweep: database inventory failed")
        report["errors"].append(f"database inventory failed: {e}")

    # --- Keycloak realms and clients ---
    try:
        url = keycloak_url or settings.KEYCLOAK_URL
        keycloak = await create_keycloak_connector(
            keycloak_url=url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        realm_to_project = {generate_project_realm_name(p, cluster): p for p in projects}
        platform_realm = settings.KEYCLOAK_DEFAULT_REALM
        expected_platform_clients = {generate_project_platform_client_id(p, cluster) for p in projects}
        # Platform clients of system realms (e.g. OPI's own) are infrastructure.
        system_platform_clients = {generate_project_platform_client_id(r, cluster) for r in SYSTEM_REALMS}

        for realm in await keycloak.list_realms():
            if realm in SKIPPED_REALMS:
                continue

            if realm in SYSTEM_REALMS:
                report["keycloak_realms"].append(
                    {"name": realm, "classification": "system", "reason": "platform-owned realm"}
                )
                continue

            if realm == platform_realm:
                report["keycloak_realms"].append(
                    {"name": realm, "classification": "system", "reason": "platform realm"}
                )
                for client in await keycloak.list_clients(realm):
                    cid = client["client_id"]
                    if (
                        cid in BUILTIN_KEYCLOAK_CLIENTS
                        or cid in SYSTEM_KEYCLOAK_CLIENTS
                        or cid in system_platform_clients
                    ):
                        classification, reason = "system", "Keycloak built-in or platform client"
                    elif cid in expected_platform_clients:
                        classification, reason = "expected", "platform client of a live project"
                    elif cid.endswith(f"-{cluster}-platform"):
                        classification, reason = (
                            "orphan_candidate",
                            "platform client naming but no live project claims it",
                        )
                    else:
                        classification, reason = "unknown", "not a platform client pattern"
                    report["keycloak_clients"].append(
                        {
                            "realm": realm,
                            "client_id": cid,
                            "public": client["public"],
                            "classification": classification,
                            "reason": reason,
                        }
                    )
                continue

            project_name = realm_to_project.get(realm)
            if project_name is None:
                report["keycloak_realms"].append(
                    {
                        "name": realm,
                        "classification": "orphan_candidate",
                        "reason": "no live project maps to this realm (realm deletion is manual; not confirmable)",
                    }
                )
                continue

            report["keycloak_realms"].append(
                {"name": realm, "classification": "expected", "reason": f"realm of live project '{project_name}'"}
            )
            for client in await keycloak.list_clients(realm):
                classification, reason = _classify_project_realm_client(
                    client["client_id"], project_name, projects.get(project_name, [])
                )
                report["keycloak_clients"].append(
                    {
                        "realm": realm,
                        "client_id": client["client_id"],
                        "public": client["public"],
                        "classification": classification,
                        "reason": reason,
                    }
                )
    except Exception as e:
        logger.exception("Orphan sweep: Keycloak inventory failed")
        report["errors"].append(f"keycloak inventory failed: {e}")

    # --- MinIO buckets ---
    try:
        from opi.core.cluster_config import get_minio_host, get_minio_port

        minio = create_minio_connector()
        alias = "orphan-sweep"
        minio_url = (
            f"{'https' if settings.MINIO_USE_TLS else 'http'}://{get_minio_host(cluster)}:{get_minio_port(cluster)}"
        )
        await minio.configure_alias(alias, minio_url, settings.MINIO_ADMIN_ACCESS_KEY, settings.MINIO_ADMIN_SECRET_KEY)

        for bucket in await minio.list_buckets(alias):
            bucket_name = bucket.get("name", "") if isinstance(bucket, dict) else str(bucket)
            if not bucket_name:
                continue
            if bucket_name.startswith("backup-"):
                classification, reason = "system", "backup destination bucket"
            elif (bucket_name, cluster) in expected["minio_bucket"]:
                classification, reason = "expected", "belongs to a live deployment"
            else:
                classification, reason = "unknown", "does not match any project naming convention"
                for project_name in projects:
                    prefix = _sanitize_for_lowercase(project_name)
                    if bucket_name == prefix or bucket_name.startswith(f"{prefix}-"):
                        classification = "orphan_candidate"
                        reason = f"matches project '{project_name}' naming but no live deployment claims it"
                        break
            report["minio_buckets"].append({"name": bucket_name, "classification": classification, "reason": reason})
    except Exception as e:
        logger.exception("Orphan sweep: MinIO inventory failed")
        report["errors"].append(f"minio inventory failed: {e}")

    # --- Stale / wrong marks ---
    try:
        service = MarkedForDeletionService()
        for mark in await service.get_all_marks():
            rtype = mark["resource_type"]
            rname = mark["resource_name"]
            rcluster = mark["cluster"]
            if rtype in expected and (rname, rcluster) in expected[rtype]:
                report["stale_marks"].append(
                    {
                        "type": rtype,
                        "name": rname,
                        "cluster": rcluster,
                        "reason": "resource is in the expected set - mark is wrong, should be unmarked",
                    }
                )
            elif (
                rtype in ("postgresql_database", "postgresql_user")
                and actual_databases
                and rname not in actual_databases
            ):
                report["stale_marks"].append(
                    {
                        "type": rtype,
                        "name": rname,
                        "cluster": rcluster,
                        "reason": "resource no longer exists - mark is stale, purge would no-op",
                    }
                )
    except Exception as e:
        logger.exception("Orphan sweep: mark analysis failed")
        report["errors"].append(f"mark analysis failed: {e}")

    report["summary"] = _summarize(report)
    return report


def _summarize(report: dict[str, Any]) -> dict[str, int]:
    """Count classifications across all sections."""
    counts: dict[str, int] = {}
    for section in ("databases", "keycloak_realms", "keycloak_clients", "minio_buckets"):
        for entry in report[section]:
            key = entry["classification"]
            counts[key] = counts.get(key, 0) + 1
    counts["stale_marks"] = len(report["stale_marks"])
    return counts
