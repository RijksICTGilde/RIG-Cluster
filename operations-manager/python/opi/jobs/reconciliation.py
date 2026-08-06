"""Reconciliation job for cleaning up orphaned resources.

Compares expected resources (derived from project YAML files) against actual
resources and the ``marked_for_deletion`` table. Resources that are both
orphaned AND marked past the configurable grace period are purged.

Deletion ordering:
1. PostgreSQL databases and users (external to cluster)
2. MinIO buckets, users, and policies (external to cluster)
3. Backup data (S3 prefixes/buckets on backup destination)
4. PVCs (require namespace to still exist)
5. Namespaces (only when all conditions are met)
"""

import logging
import os
from typing import TYPE_CHECKING, Any

from opi.connectors.minio_mc import MinioConnector, create_minio_connector
from opi.connectors.postgres import PostgresConnector, create_postgres_connector
from opi.core.config import settings
from opi.services.marked_for_deletion_service import MarkedForDeletionService
from opi.services.project_store import get_project_store
from opi.utils.naming import (
    generate_backup_prefix,
    generate_bucket_name,
    generate_database_name,
    generate_minio_policy_name,
    generate_minio_username,
)

if TYPE_CHECKING:
    from opi.handlers.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)


_LEGACY_DB_ALIASES = {"database", "postgresql"}
_LEGACY_MINIO_ALIASES = {"minio", "object-storage"}


def _deployment_level_service_names(deployment: dict[str, Any]) -> set[str]:
    """Service names from the deployment-level ``services`` block.

    Both v1 projects and migrated v2 files carry these entries (they also
    hold the database generation metadata). Entries are plain strings,
    ``{reference: name, ...}`` dicts, or ``{name: {config}}`` dicts.
    """
    from opi.services.services import service_entry_name

    names: set[str] = set()
    services = deployment.get("services")
    if not isinstance(services, list):
        return names
    for svc in services:
        name = service_entry_name(svc)
        if name is not None:
            names.add(name)
    return names


def _deployment_uses(
    handler: ProjectFileHandler,
    project_data: dict[str, Any],
    deployment: dict[str, Any],
    service_types: list[str],
    legacy_aliases: set[str],
) -> bool:
    """Check service usage via BOTH the catalog resolution and the deployment-level block.

    The expected set must err on the inclusive side: a service missed here makes
    a live resource look orphaned. ``deployment_uses_service`` resolves the
    schema-v2 truth (components/helm-charts/helmfiles); the deployment-level
    block covers v1 files and migration leftovers that are still authoritative
    for generation metadata.
    """
    if _deployment_level_service_names(deployment) & (set(service_types) | legacy_aliases):
        return True
    return handler.deployment_uses_service(project_data, deployment.get("name", ""), service_types)


def _build_expected_resources(project_yamls: list[dict[str, Any]]) -> dict[str, set[tuple[str, str]]]:
    """Build a set of expected (resource_name, cluster) tuples from project YAML definitions.

    Uses (resource_name, cluster) tuples instead of bare resource names to avoid
    false matches when two clusters have resources with the same generated name.

    Services are resolved the same way the delete flow does it
    (``ProjectFileHandler.deployment_uses_service``: catalog components,
    helm-charts, helmfiles) plus the deployment-level services block, so the
    expected set works for v1, v2 and v2.2 project files alike.

    Args:
        project_yamls: List of parsed project YAML dicts.

    Returns:
        Dict mapping resource_type to a set of (resource_name, cluster) tuples.
    """
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.services.services import ServiceType

    handler = ProjectFileHandler()
    db_types = [ServiceType.POSTGRESQL_DATABASE.value, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value]
    minio_types = [ServiceType.MINIO_STORAGE.value]

    # keycloak_client is intentionally absent: it is only ever marked through
    # the orphan-sweep confirm endpoint, which re-validates against a fresh sweep
    # before marking, so it relies on that gate rather than the purge-time unmark
    # re-protection that the resource types below get. Per-realm enumeration of
    # the clients a project legitimately owns has no cheap source here.
    expected: dict[str, set[tuple[str, str]]] = {
        "postgresql_database": set(),
        "postgresql_user": set(),
        "minio_bucket": set(),
        "minio_user": set(),
        "minio_policy": set(),
        "backup_data": set(),
    }

    for project in project_yamls:
        project_name = project.get("name", "")
        for deployment in project.get("deployments", []):
            deployment_name = deployment.get("name", "")
            cluster = deployment.get("cluster", "")

            if _deployment_uses(handler, project, deployment, db_types, _LEGACY_DB_ALIASES):
                # The database name carries the clone/restore generation suffix
                # (_vN); the username never does (see DatabaseManager). The
                # generation is stored under whichever DB service the deployment
                # uses — central (POSTGRESQL_DATABASE) or in-namespace
                # (NAMESPACE_POSTGRESQL_DATABASE) — so resolve against both,
                # otherwise a cloned namespace-postgres DB resolves to its base
                # name and its live _vN database falls out of the expected set.
                generation = handler.get_deployment_service_generation(
                    project, deployment_name, ServiceType.POSTGRESQL_DATABASE.value
                )
                if generation is None:
                    generation = handler.get_deployment_service_generation(
                        project, deployment_name, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                    )
                db_name = generate_database_name(project_name, deployment_name, generation)
                expected["postgresql_database"].add((db_name, cluster))
                expected["postgresql_user"].add((generate_database_name(project_name, deployment_name), cluster))

            if _deployment_uses(handler, project, deployment, minio_types, _LEGACY_MINIO_ALIASES):
                expected["minio_bucket"].add((generate_bucket_name(project_name, deployment_name), cluster))
                expected["minio_user"].add((generate_minio_username(project_name, deployment_name), cluster))
                expected["minio_policy"].add((generate_minio_policy_name(project_name, deployment_name), cluster))

            # Build expected backup_data resource name (matches marking format)
            base_namespace = deployment.get("namespace", "")
            if base_namespace and cluster:
                from opi.core.cluster_config import get_prefixed_namespace
                from opi.manager.backup.base import get_backup_bucket_name

                namespace = get_prefixed_namespace(cluster, base_namespace)
                backup_bucket = get_backup_bucket_name(project_name, cluster)
                backup_prefix = generate_backup_prefix(cluster, namespace)
                expected["backup_data"].add((f"{backup_bucket}/{backup_prefix}", cluster))

    return expected


async def cleanup_project(
    project_name: str,
    grace_period_days: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Purge expired marked resources for a specific project.

    This is the public entry point for project-scoped cleanup, used by the
    admin API. It reuses the same purge helpers as the full reconciliation job.

    Args:
        project_name: Project whose expired marks should be purged.
        grace_period_days: Override for the grace period. Uses config default if None.
        dry_run: If True, log actions but do not actually purge resources.

    Returns:
        Summary dict with purged/error lists.
    """
    if grace_period_days is None:
        grace_period_days = settings.DELETION_GRACE_PERIOD_DAYS

    service = MarkedForDeletionService()

    project_expired = await service.get_expired_marks(grace_period_days, project_name=project_name)

    results: dict[str, Any] = {
        "project_name": project_name,
        "grace_period_days": grace_period_days,
        "dry_run": dry_run,
        "purged": [],
        "errors": [],
    }

    if not project_expired:
        return results

    # Same expected-set protection as the full reconcile: a mark whose
    # resource is back in the project YAMLs must never be purged.

    all_projects = get_project_store().get_all()
    expected = _build_expected_resources([p.data for p in all_projects if p.data])

    await _purge_marks(project_expired, service, results, dry_run, expected=expected)

    logger.info(
        "Cleanup for project '%s': purged=%d, errors=%d, dry_run=%s",
        project_name,
        len(results["purged"]),
        len(results["errors"]),
        dry_run,
    )

    return results


async def _purge_marks(
    marks: list[dict],
    service: MarkedForDeletionService,
    results: dict[str, Any],
    dry_run: bool,
    expected: dict[str, set[tuple[str, str]]] | None = None,
) -> None:
    """Purge a list of marks in the correct dependency order.

    Shared by both ``reconcile()`` and ``cleanup_project()``.

    When *expected* is given, marks whose resource is in the current expected
    set are unmarked instead of purged — re-checked here, at purge time, so a
    service that was deselected and later re-added can never lose its resource
    even if the mark survived (the waggl-9et scenario).
    """
    if expected is not None:
        protected = [m for m in marks if (m["resource_name"], m["cluster"]) in expected.get(m["resource_type"], set())]
        if protected:
            results.setdefault("unmarked", [])
            for mark in protected:
                logger.warning(
                    "Refusing to purge %s '%s' (cluster %s): resource is in the current expected set - unmarking",
                    mark["resource_type"],
                    mark["resource_name"],
                    mark["cluster"],
                )
                if not dry_run:
                    await service.delete_mark(mark["id"])
                results["unmarked"].append(
                    {"type": mark["resource_type"], "name": mark["resource_name"], "cluster": mark["cluster"]}
                )
            marks = [m for m in marks if m not in protected]

    # Group by type for ordered deletion
    db_marks = [m for m in marks if m["resource_type"] == "postgresql_database"]
    db_user_marks = [m for m in marks if m["resource_type"] == "postgresql_user"]
    bucket_marks = [m for m in marks if m["resource_type"] == "minio_bucket"]
    minio_user_marks = [m for m in marks if m["resource_type"] == "minio_user"]
    minio_policy_marks = [m for m in marks if m["resource_type"] == "minio_policy"]
    namespace_marks = [m for m in marks if m["resource_type"] == "namespace"]
    backup_marks = [m for m in marks if m["resource_type"] == "backup_data"]

    # PostgreSQL databases and users
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

    # MinIO resources (policies first, then users, then buckets)
    if minio_policy_marks or minio_user_marks or bucket_marks:
        try:
            minio_conn = create_minio_connector()
            alias = "default-minio"
            from opi.core.cluster_config import get_minio_host, get_minio_port

            # This passed None, which get_cluster_config rejects outright, so the whole
            # MinIO purge branch raised before it purged anything -- swallowed by the
            # except below as "failed to initialize". This instance only manages its own
            # cluster, so that is the one to configure the alias against.
            minio_host = get_minio_host(settings.CLUSTER_MANAGER)
            minio_port = get_minio_port(settings.CLUSTER_MANAGER)
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

    # Keycloak clients (confirmed orphans from the service-orphan sweep)
    keycloak_client_marks = [m for m in marks if m["resource_type"] == "keycloak_client"]
    for mark in keycloak_client_marks:
        await _purge_keycloak_client(mark, service, results, dry_run)

    # Backup data (Kopia snapshots)
    for mark in backup_marks:
        await _purge_backup_data(mark, service, results, dry_run)

    # PVCs (remove manifest from git, regenerate kustomization, let ArgoCD prune)
    pvc_marks = [m for m in marks if m["resource_type"] == "pvc"]
    for mark in pvc_marks:
        await _purge_pvc(mark, service, results, dry_run)

    # Deployment manifests (deferred from failed ArgoCD app deletion)
    manifest_marks = [m for m in marks if m["resource_type"] == "deployment_manifests"]
    for mark in manifest_marks:
        await _purge_deployment_manifests(mark, service, results, dry_run)

    # Namespaces (only when all conditions met)
    for mark in namespace_marks:
        await _purge_namespace(mark, service, results, dry_run)


async def reconcile(
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
        project_yamls: List of all parsed project YAML dicts.
        grace_period_days: Override for the grace period. Uses config default if None.
        dry_run: If True, log actions but do not actually purge resources.

    Returns:
        Summary dict with counts of actions taken.
    """
    if grace_period_days is None:
        grace_period_days = settings.DELETION_GRACE_PERIOD_DAYS

    service = MarkedForDeletionService()
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

        if rtype in expected and (rname, cluster) in expected[rtype]:
            logger.info(
                "Resource %s '%s' on cluster '%s' is back in expected set - unmarking (restored via git revert?)",
                rtype,
                rname,
                cluster,
            )
            if not dry_run:
                await service.unmark_resource(rtype, rname, cluster)
            results["unmarked"].append({"type": rtype, "name": rname, "cluster": cluster})

    # --- Step 2: Purge expired marks (re-protected against the expected set) ---
    expired_marks = await service.get_expired_marks(grace_period_days)
    await _purge_marks(expired_marks, service, results, dry_run, expected=expected)

    # --- Step 3: Orphan detection is deliberately NOT automated here ---
    # Actual-resource scanning lives in opi.jobs.service_orphan_sweep (report-
    # first by design): GET /api/v2/admin/orphans/report produces a classified
    # inventory, POST /api/v2/admin/orphans/confirm marks human-confirmed
    # candidates, after which this job purges them past the grace period.
    # Auto-marking from a scan is forbidden: a wrong expected set would
    # schedule live resources for deletion (see the waggl-9et near-miss).

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
    """Purge a PostgreSQL database that has passed the grace period.

    Refuses when the database has active connections: a marked-but-in-use
    database means our administration is out of sync with reality, and
    ``delete_database`` would terminate those connections before dropping.
    The mark is kept and reported for investigation instead.
    """
    db_name = mark["resource_name"]
    try:
        active = await connector.count_active_connections(db_name)
        if active > 0:
            msg = (
                f"Refusing to purge PostgreSQL database '{db_name}': "
                f"{active} active connection(s) - marked but in use, investigate before deleting"
            )
            logger.warning(msg)
            results.setdefault("refused", []).append({"type": "postgresql_database", "name": db_name, "reason": msg})
            return

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


async def _purge_keycloak_client(
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a Keycloak client that was confirmed via the orphan sweep.

    The realm is stored in the mark metadata at confirm time. A client that
    is already gone counts as purged (the goal state is reached).
    """
    client_id = mark["resource_name"]
    metadata = mark.get("metadata", {})
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    realm = metadata.get("realm", "")

    if not realm:
        error_msg = f"Keycloak client mark '{client_id}' has no realm in metadata - cannot delete"
        logger.warning(error_msg)
        results["errors"].append(error_msg)
        return

    try:
        if dry_run:
            logger.info("[DRY RUN] Would purge Keycloak client: %s (realm %s)", client_id, realm)
            results["purged"].append({"type": "keycloak_client", "name": client_id, "realm": realm})
            return

        from opi.connectors.keycloak import create_keycloak_connector

        keycloak = await create_keycloak_connector(
            keycloak_url=settings.KEYCLOAK_URL,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )
        deleted = await keycloak.delete_client_by_client_id(realm, client_id)
        if not deleted:
            logger.info("Keycloak client '%s' already gone from realm '%s'", client_id, realm)
        await service.delete_mark(mark["id"])
        results["purged"].append({"type": "keycloak_client", "name": client_id, "realm": realm})
    except Exception as e:
        error_msg = f"Failed to purge Keycloak client '{client_id}' in realm '{realm}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_backup_data(
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge backup data (Kopia snapshots) for a deleted deployment.

    Lists all snapshots in the Kopia repository and deletes them individually.
    The Kopia connection details (bucket, prefix, password) are stored in the
    mark metadata at the time the resource was marked for deletion.
    """
    resource_name = mark["resource_name"]
    metadata = mark.get("metadata", {})
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)

    s3_bucket = metadata.get("s3_bucket")
    s3_prefix = metadata.get("s3_prefix")
    kopia_password = metadata.get("kopia_password")

    # S3 connection details are read from current settings rather than stored
    # in mark metadata, to avoid persisting credentials in the database.
    s3_endpoint = settings.BACKUP_S3_ENDPOINT
    s3_access_key = settings.BACKUP_S3_ACCESS_KEY
    s3_secret_key = settings.BACKUP_S3_SECRET_KEY
    s3_use_tls = settings.BACKUP_S3_USE_TLS

    if not all([s3_bucket, s3_prefix, s3_endpoint, kopia_password]):
        error_msg = (
            f"Incomplete backup metadata for '{resource_name}' - "
            "cannot connect to Kopia repository (manual cleanup required). "
            f"Mark '{mark['id']}' retained for visibility in admin API."
        )
        logger.warning(error_msg)
        results["errors"].append(error_msg)
        return

    try:
        from opi.connectors.kopia import KopiaConnector, KopiaRepositoryConfig

        if not KopiaConnector.is_kopia_available:
            error_msg = f"Kopia CLI not available - cannot purge backup data for '{resource_name}'"
            logger.warning(error_msg)
            results["errors"].append(error_msg)
            return

        repo_config = KopiaRepositoryConfig(
            s3_endpoint=s3_endpoint,
            s3_bucket=s3_bucket,
            s3_access_key=s3_access_key,
            s3_secret_key=s3_secret_key,
            s3_prefix=s3_prefix,
            password=kopia_password,
            use_tls=s3_use_tls,
        )

        kopia = KopiaConnector()
        snapshots = await kopia.list_snapshots(repo_config)

        if not snapshots:
            logger.info("No snapshots found for '%s' - marking as purged", resource_name)
            if not dry_run:
                await service.delete_mark(mark["id"])
            results["purged"].append({"type": "backup_data", "name": resource_name, "snapshots_deleted": 0})
            return

        if dry_run:
            logger.info(
                "[DRY RUN] Would purge %d backup snapshot(s) for '%s'",
                len(snapshots),
                resource_name,
            )
            results["purged"].append(
                {"type": "backup_data", "name": resource_name, "snapshots_deleted": len(snapshots)}
            )
            return

        deleted_count = 0
        for snapshot in snapshots:
            try:
                success = await kopia.delete_snapshot(repo_config, snapshot.snapshot_id)
                if success:
                    deleted_count += 1
                else:
                    logger.warning(
                        "Failed to delete snapshot %s for '%s'",
                        snapshot.snapshot_id,
                        resource_name,
                    )
            except Exception as e:
                logger.warning(
                    "Error deleting snapshot %s for '%s': %s",
                    snapshot.snapshot_id,
                    resource_name,
                    e,
                )

        if deleted_count == len(snapshots):
            await service.delete_mark(mark["id"])
            logger.info(
                "Purged all %d backup snapshot(s) for '%s'",
                deleted_count,
                resource_name,
            )
        else:
            logger.warning(
                "Only purged %d/%d backup snapshot(s) for '%s' - keeping mark for retry",
                deleted_count,
                len(snapshots),
                resource_name,
            )
        results["purged"].append({"type": "backup_data", "name": resource_name, "snapshots_deleted": deleted_count})

    except Exception as e:
        error_msg = f"Failed to purge backup data for '{resource_name}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_pvc(
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge a PVC by removing its manifest from the deployment git repo.

    The manifest file was previously renamed with a ``.marked-for-deletion.yaml``
    suffix by ``PVCManager.handle_service_removal``.  This function:

    1. Checks out the deployment git repo via a ProjectManager.
    2. Deletes the marked manifest file.
    3. Regenerates ``kustomization.yaml`` so the file is no longer listed.
    4. Commits and pushes - ArgoCD will then prune the PVC.
    """
    resource_name = mark["resource_name"]
    project_name = mark["project_name"]
    metadata = mark.get("metadata", {})
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)

    deployment_path = metadata.get("deployment_path", "")
    deployment_name = mark.get("deployment_name", "")

    if not deployment_path:
        error_msg = (
            f"PVC mark '{resource_name}' for project '{project_name}' "
            "has no deployment_path in metadata - cannot locate manifest file"
        )
        logger.warning(error_msg)
        results["errors"].append(error_msg)
        return

    try:
        if dry_run:
            logger.info("[DRY RUN] Would purge PVC manifest: %s (project=%s)", resource_name, project_name)
            results["purged"].append({"type": "pvc", "name": resource_name})
            return

        from opi.manager.project_manager import ProjectManager

        project = get_project_store().get(project_name)
        if not project:
            error_msg = f"Project '{project_name}' not found - cannot purge PVC manifest '{resource_name}'"
            logger.warning(error_msg)
            results["errors"].append(error_msg)
            return

        async with ProjectManager(project_file_relative_path=f"projects/{project.filename}") as pm:
            project_data = await pm.get_contents()
            repositories = project_data.get("repositories", [])
            repo_config = repositories[0] if repositories else {}
            repo_name = repo_config.get("name", "") if isinstance(repo_config, dict) else ""

            git_connector = await pm.get_git_connector_for_deployment(repo_name, repo_config)

            working_dir = await git_connector.get_working_dir()
            full_output_dir = os.path.join(working_dir, deployment_path)
            manifest_path = os.path.join(full_output_dir, resource_name)

            if not os.path.exists(manifest_path):
                logger.info(
                    "PVC manifest '%s' already removed from git - cleaning up mark",
                    resource_name,
                )
                await service.delete_mark(mark["id"])
                results["purged"].append({"type": "pvc", "name": resource_name})
                return

            # Delete the manifest file
            os.remove(manifest_path)
            logger.info("Deleted PVC manifest: %s", manifest_path)

            # Regenerate kustomization.yaml so the deleted file is no longer listed
            sops_files, regular_files = pm._manifest_generator.collect_manifest_files(
                full_output_dir, include_subfolders=False
            )
            pm._manifest_generator.create_kustomization_files(
                output_dir=full_output_dir,
                sops_files=sops_files,
                regular_files=regular_files,
            )
            logger.info("Regenerated kustomization.yaml for %s", deployment_path)

            # Commit and push
            await git_connector.commit_and_push(
                f"Purge deferred PVC manifest {resource_name} for {project_name}/{deployment_name}"
            )

            await service.delete_mark(mark["id"])
            logger.info("Purged PVC manifest: %s (project=%s)", resource_name, project_name)

        results["purged"].append({"type": "pvc", "name": resource_name})

    except Exception as e:
        error_msg = f"Failed to purge PVC manifest '{resource_name}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_deployment_manifests(
    mark: dict,
    service: MarkedForDeletionService,
    results: dict[str, list],
    dry_run: bool,
) -> None:
    """Purge deployment manifests that were deferred because ArgoCD app deletion timed out.

    Only deletes manifests if the ArgoCD application is confirmed gone.
    If the app still exists, the mark is kept for the next reconciliation run.
    """
    resource_name = mark["resource_name"]
    project_name = mark["project_name"]
    deployment_name = mark["deployment_name"]
    cluster = mark["cluster"]
    metadata = mark.get("metadata", {})
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)

    repository_name = metadata.get("repository_name", "")
    argocd_app_name = metadata.get("argocd_app_name", "")

    try:
        # Check if the ArgoCD application is still alive
        from opi.connectors import create_argo_connector

        argo_connector = create_argo_connector()
        try:
            app_exists = await argo_connector.application_exists(argocd_app_name)
        except PermissionError:
            app_exists = False  # AppProject gone = app gone

        if app_exists:
            logger.info(
                "ArgoCD application '%s' still exists - keeping manifests for '%s' (will retry next run)",
                argocd_app_name,
                resource_name,
            )
            return

        logger.info(
            "ArgoCD application '%s' is gone - proceeding with manifest cleanup for '%s'",
            argocd_app_name,
            resource_name,
        )

        if dry_run:
            logger.info("[DRY RUN] Would purge deployment manifests: %s", resource_name)
            results["purged"].append({"type": "deployment_manifests", "name": resource_name})
            return

        # Look up the project to get the git connector
        from opi.manager.project_manager import ProjectManager
        from opi.utils.naming import generate_deployment_manifest_path

        project = get_project_store().get(project_name)
        if not project:
            logger.warning(
                "Project '%s' not found - cannot clean up manifests for '%s'. Removing mark.",
                project_name,
                resource_name,
            )
            await service.delete_mark(mark["id"])
            results["purged"].append({"type": "deployment_manifests", "name": resource_name})
            return

        async with ProjectManager(project_file_relative_path=f"projects/{project.filename}") as pm:
            project_data = await pm.get_contents()
            repositories = project_data.get("repositories", [])
            repo_config = None
            for repo in repositories:
                if repo.get("name") == repository_name:
                    repo_config = repo
                    break

            if not repo_config:
                logger.warning(
                    "Repository '%s' not found in project '%s' - cannot clean up manifests. Removing mark.",
                    repository_name,
                    project_name,
                )
                await service.delete_mark(mark["id"])
                results["purged"].append({"type": "deployment_manifests", "name": resource_name})
                return

            manifest_connector = await pm.get_git_connector_for_deployment(repository_name, repo_config)
            repo_path = repo_config.get("path", "")
            deployment_folder_path = generate_deployment_manifest_path(
                cluster, project_name, deployment_name, repo_path
            )

            await manifest_connector.ensure_repo_cloned()
            folder_full_path = os.path.join(await manifest_connector.get_working_dir(), deployment_folder_path)

            if os.path.exists(folder_full_path):
                import shutil

                shutil.rmtree(folder_full_path)
                commit_message = (
                    f"Deferred cleanup: delete deployment '{deployment_name}' manifests from project '{project_name}'"
                )
                await manifest_connector.commit_and_push_changes(commit_message)
                logger.info("Purged deployment manifests: %s", deployment_folder_path)
            else:
                logger.info("Deployment manifests already removed: %s", deployment_folder_path)

        await service.delete_mark(mark["id"])
        results["purged"].append({"type": "deployment_manifests", "name": resource_name})

    except Exception as e:
        error_msg = f"Failed to purge deployment manifests '{resource_name}': {e}"
        logger.exception(error_msg)
        results["errors"].append(error_msg)


async def _purge_namespace(
    mark: dict,
    service: MarkedForDeletionService,
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
