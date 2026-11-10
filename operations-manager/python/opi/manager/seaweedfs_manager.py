"""Object storage of the seaweedfs-storage service (RC-331, RC-381).

Smaller than ``MinioManager`` and not missing anything: management runs over ``weed shell``
rather than ``mc admin``, so a bucket, an identity and a policy are each one command. There
are no bucket generations here, unlike MinIO, where they exist for a restore into a new
bucket.

A clone copies the data with ``mc`` over the S3 port, on the admin key pair from
``seaweedfs-credentials``; identity management never goes that way.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.connectors.minio_mc import create_minio_connector
from opi.connectors.seaweedfs_shell import (
    SeaweedfsConfigurationError,
    SeaweedfsConnector,
    SeaweedfsExecutionError,
    create_seaweedfs_connector,
)
from opi.core.cluster_config import get_prefixed_namespace, get_seaweedfs_host, get_seaweedfs_port
from opi.core.config import settings
from opi.manager.backup.runs import clone_backup_items
from opi.manager.clone_validation import resolve_clone_source_deployment
from opi.services import ServiceType
from opi.services.catalog.shared.noncurrent_versions import deployment_noncurrent_rule
from opi.services.secrets import SeaweedFSSecret
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import CloneFromType
from opi.utils.naming import generate_bucket_name, generate_minio_policy_name, generate_minio_username
from opi.utils.passwords import generate_secure_password

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

logger = logging.getLogger(__name__)

#: The mc alias a clone's bucket copy runs on. One alias, because source and target sit on
#: the same server; a clone from another S3 server is a migration.
MC_ALIAS = "seaweedfs-admin"

#: The resource types a switched-off service is listed under in ``marked_for_deletion``.
#: Names of their own next to MinIO's, because the naming functions are the same: a bucket of
#: either service carries the same name and only the type says where it stands.
MARKED_RESOURCE_TYPES = ("seaweedfs_bucket", "seaweedfs_user", "seaweedfs_policy")


class SeaweedfsManager:
    """Bucket, identity and policy per deployment, in SeaweedFS."""

    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager

    def _deployment_uses_seaweedfs(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data, deployment_name, [ServiceType.SEAWEEDFS_STORAGE.value]
        )

    @staticmethod
    def _versioning_setting(project_data: dict[str, Any]) -> bool | None:
        """``enable-versioning`` from the project block, or None when nobody chose it.

        Read form-agnostically, like ``MinioManager._get_minio_service_config``: the entry can
        be a bare string, a legacy dict or a record.
        """
        for entry in project_data.get("services", []) or []:
            if service_entry_name(entry) != ServiceType.SEAWEEDFS_STORAGE.value:
                continue
            config = service_entry_config(entry) or {}
            value = config.get("enable-versioning")
            return None if value is None else bool(value)
        return None

    @staticmethod
    async def _limit_noncurrent_versions(
        connector: SeaweedfsConnector, bucket: str, project_data: dict[str, Any], deployment_name: str
    ) -> None:
        """Put the limit on noncurrent versions on a bucket that has versioning (RC-380).

        Runs on a bucket where versioning was turned on outside OPI too: without a rule such
        a bucket keeps every old version. A bucket without versioning has no noncurrent
        versions, so it gets no rule.

        Raises:
            RuntimeError: the rule cannot be set.
        """
        if not await connector.get_versioning(bucket):
            logger.debug("Bucket %s heeft geen versioning, dus geen grens op oude versies", bucket)
            return

        rule = deployment_noncurrent_rule(project_data, deployment_name, ServiceType.SEAWEEDFS_STORAGE)
        try:
            outcome = await connector.ensure_noncurrent_version_rule(bucket, rule.max_versions, rule.expire_days)
        except (SeaweedfsConfigurationError, SeaweedfsExecutionError) as e:
            raise RuntimeError(f"de grens op oude versies van bucket {bucket} zetten mislukte: {e}") from e

        logger.info(
            "Bucket %s houdt hoogstens %s oude versies per object%s (%s)",
            bucket,
            rule.max_versions,
            "" if rule.expire_days is None else f", hoogstens {rule.expire_days} dagen oud",
            outcome["status"],
        )

    async def _existing_keys(self, deployment: dict[str, Any]) -> SeaweedFSSecret | None:
        """The keys from the k8s secret, when they are there.

        ``BaseSecret.get_data`` is the generic form of what ``MinioManager`` does with a
        private helper of its own, so it is reused here.
        """
        namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])
        return await SeaweedFSSecret.get_data(self.project_manager._kubectl_connector, namespace, deployment["name"])

    @staticmethod
    def _s3_endpoint(cluster: str) -> str:
        host = get_seaweedfs_host(cluster)
        if host is None:
            raise RuntimeError(
                f"cluster '{cluster}' levert seaweedfs-storage niet; de dienst hoort hier niet aan te staan"
            )
        return f"http://{host}:{get_seaweedfs_port(cluster)}"

    @staticmethod
    async def _require_closed_s3_port(connector: SeaweedfsConnector, cluster: str) -> None:
        """Refuse to provision while the S3 port is open anonymously.

        Without an admin identity SeaweedFS lets every call through (row G14), and a bucket
        created then would be readable for the whole cluster. That is an error and not a
        warning, because no project may be put in that state.
        """
        endpoint = SeaweedfsManager._s3_endpoint(cluster)
        if await connector.anonymous_access_allowed(endpoint):
            raise RuntimeError(
                f"de S3-poort van SeaweedFS op {endpoint} neemt een ongeauthenticeerde aanroep aan, "
                "dus er is geen admin-identiteit; controleer het secret seaweedfs-credentials. "
                "Een bucket aanmaken zou hem voor de hele cluster leesbaar maken."
            )

    async def create_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any], force_clone: bool = False
    ) -> None:
        """Bucket, identity, policy, versioning, and a clone of the data when asked for."""
        deployment_name = deployment["name"]
        if not self._deployment_uses_seaweedfs(project_data, deployment_name):
            logger.debug("Deployment %s gebruikt seaweedfs-storage niet, overgeslagen", deployment_name)
            return

        cluster = deployment["cluster"]
        host = get_seaweedfs_host(cluster)
        if host is None:
            raise RuntimeError(
                f"cluster '{cluster}' levert seaweedfs-storage niet; de dienst hoort hier niet aan te staan"
            )

        project_name = await self.project_manager.get_name()
        user = generate_minio_username(project_name, deployment_name)
        bucket = generate_bucket_name(project_name, deployment_name)
        policy = generate_minio_policy_name(project_name, deployment_name)

        connector = create_seaweedfs_connector()
        await self._require_closed_s3_port(connector, cluster)
        existing = await self._existing_keys(deployment)
        # The identity in the secret is only trustworthy while SeaweedFS knows it too: a
        # fresh SeaweedFS next to a surviving secret is the case MinIO's
        # _validate_and_fix_credentials exists for (row G10).
        if existing is not None and await connector.user_exists(user):
            secret_key = existing.secret_key
            logger.info("Bestaande SeaweedFS-sleutels hergebruikt voor %s", deployment_name)
        else:
            secret_key = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
            await connector.delete_user(user)
            await connector.create_user(user, user, secret_key)
            logger.info("SeaweedFS-identiteit %s aangemaakt voor %s", user, deployment_name)

        await connector.create_bucket(bucket, owner=user)
        await connector.grant_bucket_access(user, bucket, policy)
        # Nothing to do when nobody chose, as with minio: a fresh bucket has it off.
        versioning = self._versioning_setting(project_data)
        if versioning is not None:
            await connector.set_versioning(bucket, versioning)
        await self._limit_noncurrent_versions(connector, bucket, project_data, deployment_name)

        self.project_manager._add_secret_to_create(
            deployment_name,
            "seaweedfs",
            SeaweedFSSecret(
                host=host,
                port=get_seaweedfs_port(cluster),
                access_key=user,
                secret_key=secret_key,
                bucket_name=bucket,
                region=settings.MINIO_REGION,
            ),
        )
        logger.info("SeaweedFS-resources klaar voor %s", deployment_name)

        await self._clone_if_requested(project_data, deployment, bucket, force_clone)

    async def _clone_if_requested(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        target_bucket: str,
        force_clone: bool,
    ) -> None:
        """Put the data of ``clone-from`` into the bucket that was just prepared.

        After provisioning and not before: bucket, identity and policy are then ready and the
        clone is only a copy, which is why this manager needs no clone variant of the
        provisioning path the way ``MinioManager`` has one.
        """
        clone_from = deployment.get("clone-from")
        if not clone_from:
            return
        deployment_name = deployment["name"]
        if not isinstance(clone_from, dict):
            raise TypeError(
                f"clone-from van deployment '{deployment_name}' moet een dict met type, reference en mode zijn, "
                f"niet {type(clone_from).__name__}"
            )

        status = clone_from.get("status")
        completed = status.get("completed", False) if isinstance(status, dict) else False
        if clone_from.get("mode", "once") == "once" and completed and not force_clone:
            logger.info("Kloon van %s overgeslagen: mode once en al afgerond", deployment_name)
            return

        kind = clone_from.get("type")
        if kind == CloneFromType.DEPLOYMENT.value:
            await self._clone_from_deployment(
                project_data, deployment, target_bucket, str(clone_from.get("reference") or "")
            )
        elif kind == CloneFromType.BACKUP.value:
            await self._clone_from_backup(project_data, deployment, target_bucket, clone_from)
        elif kind == CloneFromType.REMOTE_SOURCE.value:
            # A remote-source carries its keys under `minio-storage`, so this would be a copy
            # from MinIO to SeaweedFS: a migration, with the version history still open. See
            # `features/seaweedfs-objectopslag.md`.
            raise RuntimeError(
                f"deployment '{deployment_name}' gebruikt seaweedfs-storage met clone-from van een remote-source. "
                "Een remote-source beschrijft een MinIO-bucket, dus dat is een migratie en geen kloon; "
                "kloon van een deployment of uit een back-up werkt wel."
            )
        else:
            raise ValueError(f"onbekend clone-from type '{kind}' voor deployment '{deployment_name}'")

    async def _clone_from_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        target_bucket: str,
        source_deployment: str,
    ) -> None:
        """Copy the bucket of another deployment of this project (row K1)."""
        deployment_name = deployment["name"]
        # Through the validation and not on the name itself: an unchecked reference composes a
        # bucket name of another project.
        resolve_clone_source_deployment(project_data, source_deployment, deployment_name)
        project_name = await self.project_manager.get_name()
        source_bucket = generate_bucket_name(project_name, source_deployment)

        connector = create_seaweedfs_connector()
        if source_bucket not in await connector.list_buckets():
            raise RuntimeError(
                f"de bronbucket '{source_bucket}' van deployment '{source_deployment}' bestaat niet, dus er is niets te klonen"
            )

        await self._mirror(source_bucket, target_bucket, deployment["cluster"])
        self.project_manager.report_clone_performed(deployment_name, ServiceType.SEAWEEDFS_STORAGE.value, None)

    async def _clone_from_backup(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        target_bucket: str,
        clone_from: dict[str, Any],
    ) -> None:
        """Restore the bucket from a Kopia backup, the way ``MinioManager`` does."""
        from opi.manager.backup.bucket_backup import create_bucket_backup_manager

        deployment_name = deployment["name"]
        project_name = await self.project_manager.get_name()
        items = await clone_backup_items(project_data, project_name, deployment, clone_from)
        item = next(
            (item for item in items if isinstance(item, dict) and item.get("resource_type") == "bucket"),
            None,
        )
        if item is None:
            logger.info("Kloon uit back-up voor %s heeft geen bucket-item, niets terug te zetten", deployment_name)
            return

        cluster = deployment["cluster"]
        namespace = get_prefixed_namespace(cluster, deployment.get("namespace", project_name))
        result = await create_bucket_backup_manager().restore_bucket(
            cluster=cluster,
            namespace=namespace,
            reference_name=item.get("reference_name", ""),
            target_minio_endpoint=self._s3_endpoint(cluster),
            target_bucket_name=target_bucket,
            target_access_key=settings.SEAWEEDFS_ADMIN_ACCESS_KEY,
            target_secret_key=settings.SEAWEEDFS_ADMIN_SECRET_KEY,
            snapshot_id=item.get("snapshot_id"),
            project_name=project_name,
        )
        if not result.success:
            raise RuntimeError(f"terugzetten van bucket {target_bucket} uit de back-up mislukte: {result.error}")
        self.project_manager.report_clone_performed(deployment_name, ServiceType.SEAWEEDFS_STORAGE.value, None)

    async def _mirror(self, source_bucket: str, target_bucket: str, cluster: str) -> None:
        """The objects of one bucket into another, with `mc` on the admin key pair.

        `mc` and not `weed shell`, which has no bucket copy. Only the latest version travels,
        not the history; that is an open point in the feature document.
        """
        if not settings.SEAWEEDFS_ADMIN_ACCESS_KEY or not settings.SEAWEEDFS_ADMIN_SECRET_KEY:
            raise RuntimeError(
                "SEAWEEDFS_ADMIN_ACCESS_KEY en SEAWEEDFS_ADMIN_SECRET_KEY zijn niet gezet, "
                "dus er is geen sleutelpaar om de bucketkopie mee te doen"
            )
        mc = create_minio_connector()
        if not await mc.configure_alias(
            alias=MC_ALIAS,
            host=f"{get_seaweedfs_host(cluster)}:{get_seaweedfs_port(cluster)}",
            access_key=settings.SEAWEEDFS_ADMIN_ACCESS_KEY,
            secret_key=settings.SEAWEEDFS_ADMIN_SECRET_KEY,
            secure=False,
            region=settings.MINIO_REGION,
        ):
            raise RuntimeError(f"de mc-alias '{MC_ALIAS}' voor SeaweedFS op {cluster} kon niet gezet worden")
        await mc.mirror_bucket_cross_alias(MC_ALIAS, source_bucket, MC_ALIAS, target_bucket)
        logger.info("SeaweedFS-bucket %s gekopieerd naar %s", source_bucket, target_bucket)

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """Policy, identity and bucket gone, now.

        The deferred path runs through ``handle_service_removal`` with a marking service; this
        is what the delete flow of a whole project does, and what the purge calls after the
        retention period.
        """
        deployment_name = deployment["name"]
        result: dict[str, Any] = {
            "service": "seaweedfs",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }
        if not self.project_manager._project_file_handler.project_declares_service(
            project_data, [ServiceType.SEAWEEDFS_STORAGE.value]
        ):
            result["operations"].append(
                {"type": "seaweedfs_cleanup", "status": "skipped", "reason": "project noemt seaweedfs-storage nergens"}
            )
            return result
        if not SeaweedfsConnector.is_configured():
            result["operations"].append(
                {"type": "seaweedfs_cleanup", "status": "skipped", "reason": "cluster levert seaweedfs-storage niet"}
            )
            return result

        project_name = await self.project_manager.get_name()
        user = generate_minio_username(project_name, deployment_name)
        bucket = generate_bucket_name(project_name, deployment_name)
        policy = generate_minio_policy_name(project_name, deployment_name)

        connector = create_seaweedfs_connector()
        # In this order: the right first, then the identity, then the data. Every step
        # tolerates "does not exist", so a half cleanup is finished by running again.
        for resource_type, name, action in (
            ("seaweedfs_policy_detach", policy, connector.detach_policy(policy, user)),
            ("seaweedfs_policy", policy, connector.delete_policy(policy)),
            ("seaweedfs_user", user, connector.delete_user(user)),
            ("seaweedfs_bucket", bucket, connector.delete_bucket(bucket)),
        ):
            try:
                await action
                result["operations"].append({"type": resource_type, "target": name, "status": "deleted"})
            except Exception as e:
                result["success"] = False
                result["errors"].append(f"{resource_type} van {name} mislukte: {e}")
                logger.exception("Opruimen van %s %s mislukte", resource_type, name)
        return result

    async def handle_service_removal(
        self,
        project_name: str,
        deployment_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: MarkedForDeletionService | None = None,
    ) -> dict[str, Any]:
        """The service comes off a deployment: deferred cleanup, as with minio.

        With a ``marked_for_deletion_service`` the bucket, identity and policy go on the
        marking list and the purge in ``opi/jobs/reconciliation.py`` removes them after the
        retention period. Without that service, which is the delete flow of a whole project,
        they go at once.
        """
        if marked_for_deletion_service is None:
            result = await self.delete_resources_for_deployment(project_data, deployment_data)
            result["trigger"] = "service_removal"
            return result

        cluster = deployment_data.get("cluster", "")
        names = {
            "seaweedfs_bucket": generate_bucket_name(project_name, deployment_name),
            "seaweedfs_user": generate_minio_username(project_name, deployment_name),
            "seaweedfs_policy": generate_minio_policy_name(project_name, deployment_name),
        }
        for kind in MARKED_RESOURCE_TYPES:
            await marked_for_deletion_service.mark_resource(
                resource_type=kind,
                resource_name=names[kind],
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
            )
        logger.info(
            "SeaweedFS-resources gemarkeerd voor uitgesteld opruimen: %s (project=%s, deployment=%s)",
            names["seaweedfs_bucket"],
            project_name,
            deployment_name,
        )
        return {
            "service": "seaweedfs",
            "deployment": deployment_name,
            "trigger": "service_removal",
            "operations": [
                {
                    "type": "mark_for_deletion",
                    "resource_type": "seaweedfs_bucket",
                    "resource_name": names["seaweedfs_bucket"],
                    "status": "marked",
                }
            ],
            "success": True,
            "errors": [],
        }
