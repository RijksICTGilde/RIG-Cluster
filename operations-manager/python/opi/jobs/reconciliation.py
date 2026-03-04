"""Reconciliation job for cleaning up orphaned resources.

Compares expected resources (derived from project YAML files) against actual
resources and the ``marked_for_deletion`` table. Resources that are both
orphaned AND marked past the configurable grace period are purged.

Deletion ordering:
1. PostgreSQL databases and users (external to cluster)
2. MinIO buckets, users, and policies (external to cluster)
3. PVCs (require namespace to still exist)
4. Namespaces (only when all conditions are met)
"""

import logging
from typing import Any

from opi.connectors.minio_mc import MinioConnector, create_minio_connector
from opi.connectors.postgres import PostgresConnector, create_postgres_connector
from opi.core.config import settings
from opi.core.database_pool import DatabasePool
from opi.services.marked_for_deletion_service import MarkedForDeletionService
from opi.utils.naming import (
    generate_bucket_name,
    generate_database_name,
    generate_minio_policy_name,
    generate_minio_username,
)

logger = logging.getLogger(__name__)


def _build_expected_resources(project_yamls: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Build a set of expected resource names from project YAML definitions.

    Args:
        project_yamls: List of parsed project YAML dicts.

    Returns:
        Dict mapping resource_type to a set of expected resource names.
    """
    expected: dict[str, set[str]] = {
        "postgresql_database": set(),
        "postgresql_user": set(),
        "minio_bucket": set(),
        "minio_user": set(),
        "minio_policy": set(),
    }

    for project in project_yamls:
        project_name = project.get("name", "")
        for deployment in project.get("deployments", []):
            deployment_name = deployment.get("name", "")
            services = deployment.get("services", [])
            if not isinstance(services, list):
                continue

            for service in services:
                if not isinstance(service, dict):
                    continue
                ref = service.get("reference", "")

                if ref in ("database", "postgresql"):
                    db_name = generate_database_name(project_name, deployment_name)
                    expected["postgresql_database"].add(db_name)
                    expected["postgresql_user"].add(db_name)

                if ref in ("minio", "minio-storage", "object-storage"):
                    expected["minio_bucket"].add(generate_bucket_name(project_name, deployment_name))
                    expected["minio_user"].add(generate_minio_username(project_name, deployment_name))
                    expected["minio_policy"].add(generate_minio_policy_name(project_name, deployment_name))

    return expected


async def reconcile(
    pool: DatabasePool,
    project_yamls: list[dict[str, Any]],
    grace_period_days: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the reconciliation job.

    Steps:
    1. Build expected resource inventory from project YAMLs.
    2. Query actual resources from PostgreSQL and MinIO.
    3. Compare expected vs actual:
       - Orphaned AND marked AND past grace period -> purge.
       - Orphaned but NOT marked -> mark (first detection).
       - Marked but now in expected set (restored) -> unmark.

    Args:
        pool: Database pool for accessing the marked_for_deletion table.
        project_yamls: List of all parsed project YAML dicts.
        grace_period_days: Override for the grace period. Uses config default if None.
        dry_run: If True, log actions but do not actually purge resources.

    Returns:
        Summary dict with counts of actions taken.
    """
    if grace_period_days is None:
        grace_period_days = settings.DELETION_GRACE_PERIOD_DAYS

    service = MarkedForDeletionService(pool)
    expected = _build_expected_resources(project_yamls)

    results: dict[str, Any] = {
        "purged": [],
        "marked": [],
        "unmarked": [],
        "errors": [],
        "dry_run": dry_run,
    }

    # --- Step 1: Unmark resources that are back in expected (git revert recovery) ---
    all_marks = await service.get_all_marks()
    for mark in all_marks:
        rtype = mark["resource_type"]
        rname = mark["resource_name"]
        cluster = mark["cluster"]

        if rtype in expected and rname in expected[rtype]:
            logger.info(
                "Resource %s '%s' is back in expected set - unmarking (restored via git revert?)",
                rtype,
                rname,
            )
            if not dry_run:
                await service.unmark_resource(rtype, rname, cluster)
            results["unmarked"].append({"type": rtype, "name": rname, "cluster": cluster})

    # --- Step 2: Purge expired marks ---
    expired_marks = await service.get_expired_marks(grace_period_days)

    # Group by type for ordered deletion
    db_marks = [m for m in expired_marks if m["resource_type"] == "postgresql_database"]
    db_user_marks = [m for m in expired_marks if m["resource_type"] == "postgresql_user"]
    bucket_marks = [m for m in expired_marks if m["resource_type"] == "minio_bucket"]
    minio_user_marks = [m for m in expired_marks if m["resource_type"] == "minio_user"]
    minio_policy_marks = [m for m in expired_marks if m["resource_type"] == "minio_policy"]
    namespace_marks = [m for m in expired_marks if m["resource_type"] == "namespace"]

    # 2a. Purge PostgreSQL databases and users
    if db_marks or db_user_marks:
        try:
            postgres_conn = create_postgres_connector(
                host=settings.DATABASE_HOST,
                admin_username=settings.DATABASE_ADMIN_NAME,
                admin_password=settings.DATABASE_ADMIN_PASSWORD,
            )
            for mark in db_marks:
                await _purge_postgres_database(postgres_conn, mark, service, results, dry_run)
            for mark in db_user_marks:
                await _purge_postgres_user(postgres_conn, mark, service, results, dry_run)
        except Exception as e:
            error_msg = f"Failed to initialize PostgreSQL connector for purge: {e}"
            logger.exception(error_msg)
            results["errors"].append(error_msg)

    # 2b. Purge MinIO resources (policies first, then users, then buckets)
    if minio_policy_marks or minio_user_marks or bucket_marks:
        try:
            minio_conn = create_minio_connector()
            alias = "default-minio"
            # Ensure alias is configured
            from opi.core.cluster_config import get_minio_host, get_minio_port

            minio_host = get_minio_host(None)
            minio_port = get_minio_port(None)
            minio_url = f"{'https' if settings.MINIO_USE_TLS else 'http'}://{minio_host}:{minio_port}"
            await minio_conn.configure_alias(
                alias, minio_url, settings.MINIO_ADMIN_ACCESS_KEY, settings.MINIO_ADMIN_SECRET_KEY
            )

            for mark in minio_policy_marks:
                await _purge_minio_policy(minio_conn, alias, mark, service, results, dry_run)
            for mark in minio_user_marks:
                await _purge_minio_user(minio_conn, alias, mark, service, results, dry_run)
            for mark in bucket_marks:
                await _purge_minio_bucket(minio_conn, alias, mark, service, results, dry_run)
        except Exception as e:
            error_msg = f"Failed to initialize MinIO connector for purge: {e}"
            logger.exception(error_msg)
            results["errors"].append(error_msg)

    # 2c. Purge namespaces (only when ALL conditions met)
    for mark in namespace_marks:
        await _purge_namespace(mark, service, pool, results, dry_run)

    # --- Step 3: Detect newly orphaned resources and mark them ---
    # This step queries actual resources and marks any that are not in the expected set
    # and not already marked. This is intentionally conservative - we only mark, never
    # purge on first detection.
    # Note: Full actual-resource scanning is deferred to a future iteration since it
    # requires listing all databases/buckets and filtering by naming convention patterns.

    logger.info(
        "Reconciliation complete: purged=%d, marked=%d, unmarked=%d, errors=%d",
        len(results["purged"]),
        len(results["marked"]),
        len(results["unmarked"]),
        len(results["errors"]),
    )

    return results


async def _purge_postgres_database(
    connector: PostgresConnector,
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a PostgreSQL database that has passed the grace period."""
    db_name = mark["resource_name"]
    try:
        if dry_run:
            logger.info("[DRY RUN] Would purge PostgreSQL database: %s", db_name)
        else:
            await connector.delete_database(db_name)
            await service.delete_mark(mark["id"])
            logger.info("Purged PostgreSQL database: %s", db_name)
        results["purged"].append({"type": "postgresql_database", "name": db_name})
    except Exception as e:
        error_msg = f"Failed to purge PostgreSQL database '{db_name}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_postgres_user(
    connector: PostgresConnector,
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a PostgreSQL user that has passed the grace period."""
    username = mark["resource_name"]
    try:
        if dry_run:
            logger.info("[DRY RUN] Would purge PostgreSQL user: %s", username)
        else:
            await connector.delete_user(username)
            await service.delete_mark(mark["id"])
            logger.info("Purged PostgreSQL user: %s", username)
        results["purged"].append({"type": "postgresql_user", "name": username})
    except Exception as e:
        error_msg = f"Failed to purge PostgreSQL user '{username}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_minio_bucket(
    connector: MinioConnector,
    alias: str,
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a MinIO bucket that has passed the grace period."""
    bucket_name = mark["resource_name"]
    try:
        if dry_run:
            logger.info("[DRY RUN] Would purge MinIO bucket: %s", bucket_name)
        else:
            await connector.delete_bucket(alias, bucket_name, force=True)
            await service.delete_mark(mark["id"])
            logger.info("Purged MinIO bucket: %s", bucket_name)
        results["purged"].append({"type": "minio_bucket", "name": bucket_name})
    except Exception as e:
        error_msg = f"Failed to purge MinIO bucket '{bucket_name}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_minio_user(
    connector: MinioConnector,
    alias: str,
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a MinIO user that has passed the grace period."""
    username = mark["resource_name"]
    try:
        if dry_run:
            logger.info("[DRY RUN] Would purge MinIO user: %s", username)
        else:
            await connector.delete_user(alias, username)
            await service.delete_mark(mark["id"])
            logger.info("Purged MinIO user: %s", username)
        results["purged"].append({"type": "minio_user", "name": username})
    except Exception as e:
        error_msg = f"Failed to purge MinIO user '{username}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_minio_policy(
    connector: MinioConnector,
    alias: str,
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a MinIO policy that has passed the grace period."""
    policy_name = mark["resource_name"]
    try:
        if dry_run:
            logger.info("[DRY RUN] Would purge MinIO policy: %s", policy_name)
        else:
            await connector.remove_policy(alias, policy_name)
            await service.delete_mark(mark["id"])
            logger.info("Purged MinIO policy: %s", policy_name)
        results["purged"].append({"type": "minio_policy", "name": policy_name})
    except Exception as e:
        error_msg = f"Failed to purge MinIO policy '{policy_name}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_namespace(
    mark: dict,
    service: MarkedForDeletionService,
    pool: DatabasePool,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a namespace, but only when all safety conditions are met.

    Conditions:
    1. All PVCs marked for deletion in this namespace have been purged.
    2. No other marks reference this namespace (all resources cleaned up).
    """
    namespace = mark["resource_name"]
    cluster = mark["cluster"]

    try:
        # Check if any other marks still reference this namespace
        ns_marks = await service.get_marks_in_namespace(namespace, cluster)
        # Filter out the namespace mark itself
        other_marks = [m for m in ns_marks if m["id"] != mark["id"]]

        if other_marks:
            logger.info(
                "Skipping namespace '%s' purge - %d resource(s) still marked in this namespace",
                namespace,
                len(other_marks),
            )
            return

        if dry_run:
            logger.info("[DRY RUN] Would purge namespace: %s", namespace)
        else:
            # Import here to avoid circular imports
            from opi.connectors.kubectl import KubectlConnector

            kubectl = KubectlConnector()
            deleted = await kubectl.delete_namespace(namespace)
            if deleted:
                await service.delete_mark(mark["id"])
                logger.info("Purged namespace: %s", namespace)
            else:
                logger.info("Namespace '%s' was not found (already deleted)", namespace)
                await service.delete_mark(mark["id"])

        results["purged"].append({"type": "namespace", "name": namespace})

    except Exception as e:
        error_msg = f"Failed to purge namespace '{namespace}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)
