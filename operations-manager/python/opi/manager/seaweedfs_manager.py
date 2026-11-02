"""Manager voor de object-opslag van seaweedfs-storage (RC-331, RC-381).

Kleiner dan ``MinioManager``, en dat is geen gat: het beheer loopt over ``weed shell`` in
plaats van over ``mc admin``, dus een bucket, een identiteit en een policy zijn elk één
commando. Wat deze manager niet heeft zijn de bucketgeneraties van MinIO: die bestaan daar
voor een restore in een nieuwe bucket, en voor deze dienst is dat geen vorm die al iets
oplost.

De kloon kopieert de data met ``mc`` over de S3-poort, met het admin-sleutelpaar uit
``seaweedfs-credentials``; het beheer van identiteiten gaat nooit die weg op.
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
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import CloneFromType
from opi.utils.naming import generate_bucket_name, generate_minio_policy_name, generate_minio_username
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import SeaweedFSSecret

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

logger = logging.getLogger(__name__)

#: De mc-alias waarop de bucketkopie van een kloon loopt. Eén alias, want bron en doel
#: staan op dezelfde server; de kloon van een andere S3-server is een migratie.
MC_ALIAS = "seaweedfs-admin"

#: De resourcetypes waarmee een uitgezette dienst in ``marked_for_deletion`` staat. Eigen
#: namen naast die van MinIO: de naamfuncties zijn dezelfde, dus een bucket van beide
#: diensten heet hetzelfde en alleen het type zegt waar hij staat.
MARKED_RESOURCE_TYPES = ("seaweedfs_bucket", "seaweedfs_user", "seaweedfs_policy")


class SeaweedfsManager:
    """Bucket, identiteit en policy per deployment, in SeaweedFS."""

    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager

    def _deployment_uses_seaweedfs(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data, deployment_name, [ServiceType.SEAWEEDFS_STORAGE.value]
        )

    @staticmethod
    def _versioning_instelling(project_data: dict[str, Any]) -> bool | None:
        """``enable-versioning`` uit het projectblok, of None als niemand het koos.

        Vormvrij lezen zoals ``MinioManager._get_minio_service_config``: de entry kan een
        kale string, een legacy dict of een record zijn.
        """
        for entry in project_data.get("services", []) or []:
            if service_entry_name(entry) != ServiceType.SEAWEEDFS_STORAGE.value:
                continue
            config = service_entry_config(entry) or {}
            waarde = config.get("enable-versioning")
            return None if waarde is None else bool(waarde)
        return None

    @staticmethod
    async def _grens_op_oude_versies(
        connector: SeaweedfsConnector, bucket: str, project_data: dict[str, Any], deployment_name: str
    ) -> None:
        """De grens op oude versies op een bucket die versioning heeft (RC-380).

        Ook op een bucket waar versioning buiten OPI om aanstaat: zonder regel houdt die
        elke oude versie. Een bucket zonder versioning heeft geen oude versies en krijgt
        dus geen regel.

        Raises:
            RuntimeError: als de regel niet te zetten is.
        """
        if not await connector.get_versioning(bucket):
            logger.debug("Bucket %s heeft geen versioning, dus geen grens op oude versies", bucket)
            return

        regel = deployment_noncurrent_rule(project_data, deployment_name, ServiceType.SEAWEEDFS_STORAGE)
        try:
            uitkomst = await connector.ensure_noncurrent_version_rule(bucket, regel.max_versions, regel.expire_days)
        except (SeaweedfsConfigurationError, SeaweedfsExecutionError) as e:
            raise RuntimeError(f"de grens op oude versies van bucket {bucket} zetten mislukte: {e}") from e

        logger.info(
            "Bucket %s houdt hoogstens %s oude versies per object%s (%s)",
            bucket,
            regel.max_versions,
            "" if regel.expire_days is None else f", hoogstens {regel.expire_days} dagen oud",
            uitkomst["status"],
        )

    async def _bestaande_sleutels(self, deployment: dict[str, Any]) -> SeaweedFSSecret | None:
        """De sleutels uit het k8s-secret, als die er zijn.

        ``BaseSecret.get_data`` is de generieke vorm van wat ``MinioManager`` met een eigen
        private helper doet, dus die wordt hier hergebruikt.
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
    async def _eis_gesloten_s3_poort(connector: SeaweedfsConnector, cluster: str) -> None:
        """Weiger te provisioneren zolang de S3-poort anoniem openstaat.

        Zonder admin-identiteit laat SeaweedFS iedere aanroep door (rij G14), en dan zou een
        bucket die wij aanmaken voor de hele cluster leesbaar zijn. Dat is geen toestand om
        een project in te zetten, dus dit is een fout en geen waarschuwing.
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
        """Bucket, identiteit, policy, versioning en eventueel een kloon van de data."""
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
        gebruiker = generate_minio_username(project_name, deployment_name)
        bucket = generate_bucket_name(project_name, deployment_name)
        policy = generate_minio_policy_name(project_name, deployment_name)

        connector = create_seaweedfs_connector()
        await self._eis_gesloten_s3_poort(connector, cluster)
        bestaand = await self._bestaande_sleutels(deployment)
        # De identiteit uit het secret is alleen te vertrouwen als SeaweedFS hem ook kent:
        # een verse SeaweedFS naast een blijvend secret is precies het geval waar
        # _validate_and_fix_credentials bij MinIO voor bestaat (rij G10).
        if bestaand is not None and await connector.user_exists(gebruiker):
            secret_key = bestaand.secret_key
            logger.info("Bestaande SeaweedFS-sleutels hergebruikt voor %s", deployment_name)
        else:
            secret_key = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
            await connector.delete_user(gebruiker)
            await connector.create_user(gebruiker, gebruiker, secret_key)
            logger.info("SeaweedFS-identiteit %s aangemaakt voor %s", gebruiker, deployment_name)

        await connector.create_bucket(bucket, owner=gebruiker)
        await connector.grant_bucket_access(gebruiker, bucket, policy)
        # Niets doen als niemand iets koos, zoals bij minio: een verse bucket staat uit.
        versioning = self._versioning_instelling(project_data)
        if versioning is not None:
            await connector.set_versioning(bucket, versioning)
        await self._grens_op_oude_versies(connector, bucket, project_data, deployment_name)

        self.project_manager._add_secret_to_create(
            deployment_name,
            "seaweedfs",
            SeaweedFSSecret(
                host=host,
                port=get_seaweedfs_port(cluster),
                access_key=gebruiker,
                secret_key=secret_key,
                bucket_name=bucket,
                region=settings.MINIO_REGION,
            ),
        )
        logger.info("SeaweedFS-resources klaar voor %s", deployment_name)

        await self._kloon_indien_gevraagd(project_data, deployment, bucket, force_clone)

    async def _kloon_indien_gevraagd(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        doelbucket: str,
        force_clone: bool,
    ) -> None:
        """De data van ``clone-from`` in de net klaargezette bucket zetten.

        Na het provisioneren en niet ervoor: bucket, identiteit en policy zijn dan al klaar,
        en de kloon is alleen nog een kopie. Dat is de hele reden dat deze manager geen
        kloonvariant van het provisioneringspad nodig heeft zoals ``MinioManager``.
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
        afgerond = status.get("completed", False) if isinstance(status, dict) else False
        if clone_from.get("mode", "once") == "once" and afgerond and not force_clone:
            logger.info("Kloon van %s overgeslagen: mode once en al afgerond", deployment_name)
            return

        soort = clone_from.get("type")
        if soort == CloneFromType.DEPLOYMENT.value:
            await self._kloon_van_deployment(
                project_data, deployment, doelbucket, str(clone_from.get("reference") or "")
            )
        elif soort == CloneFromType.BACKUP.value:
            await self._kloon_uit_backup(project_data, deployment, doelbucket, clone_from)
        elif soort == CloneFromType.REMOTE_SOURCE.value:
            # Een remote-source draagt zijn sleutels onder `minio-storage`, dus dit is een
            # kopie van MinIO naar SeaweedFS: een migratie, met de versiehistorie als open
            # punt. Zie features/seaweedfs-objectopslag.md.
            raise RuntimeError(
                f"deployment '{deployment_name}' gebruikt seaweedfs-storage met clone-from van een remote-source. "
                "Een remote-source beschrijft een MinIO-bucket, dus dat is een migratie en geen kloon; "
                "kloon van een deployment of uit een back-up werkt wel."
            )
        else:
            raise ValueError(f"onbekend clone-from type '{soort}' voor deployment '{deployment_name}'")

    async def _kloon_van_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        doelbucket: str,
        bron_deployment: str,
    ) -> None:
        """De bucket van een andere deployment van dit project kopieren (rij K1)."""
        deployment_name = deployment["name"]
        # Via de validatie en niet op de naam zelf: een ongecontroleerde reference stelt een
        # bucketnaam van een ander project samen.
        resolve_clone_source_deployment(project_data, bron_deployment, deployment_name)
        project_name = await self.project_manager.get_name()
        bronbucket = generate_bucket_name(project_name, bron_deployment)

        connector = create_seaweedfs_connector()
        if bronbucket not in await connector.list_buckets():
            raise RuntimeError(
                f"de bronbucket '{bronbucket}' van deployment '{bron_deployment}' bestaat niet, dus er is niets te klonen"
            )

        await self._mirror(bronbucket, doelbucket, deployment["cluster"])
        self.project_manager.report_clone_performed(deployment_name, ServiceType.SEAWEEDFS_STORAGE.value, None)

    async def _kloon_uit_backup(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        doelbucket: str,
        clone_from: dict[str, Any],
    ) -> None:
        """De bucket uit een Kopia-back-up terugzetten, zoals ``MinioManager`` dat doet."""
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
        resultaat = await create_bucket_backup_manager().restore_bucket(
            cluster=cluster,
            namespace=namespace,
            reference_name=item.get("reference_name", ""),
            target_minio_endpoint=self._s3_endpoint(cluster),
            target_bucket_name=doelbucket,
            target_access_key=settings.SEAWEEDFS_ADMIN_ACCESS_KEY,
            target_secret_key=settings.SEAWEEDFS_ADMIN_SECRET_KEY,
            snapshot_id=item.get("snapshot_id"),
            project_name=project_name,
        )
        if not resultaat.success:
            raise RuntimeError(f"terugzetten van bucket {doelbucket} uit de back-up mislukte: {resultaat.error}")
        self.project_manager.report_clone_performed(deployment_name, ServiceType.SEAWEEDFS_STORAGE.value, None)

    async def _mirror(self, bronbucket: str, doelbucket: str, cluster: str) -> None:
        """De objecten van de ene bucket naar de andere, met `mc` op het admin-sleutelpaar.

        `mc` en niet `weed shell`: die kent geen bucketkopie. Gemeten in rij K1. De laatste
        versie gaat mee, de historie niet; dat staat als open punt in de feature-doc.
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
        await mc.mirror_bucket_cross_alias(MC_ALIAS, bronbucket, MC_ALIAS, doelbucket)
        logger.info("SeaweedFS-bucket %s gekopieerd naar %s", bronbucket, doelbucket)

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """Policy, identiteit en bucket weg, nu. Het uitgestelde pad loopt via
        ``handle_service_removal`` met een markeringsdienst; dit is wat de verwijderflow van
        een heel project doet, en wat de purge na de bewaartermijn aanroept."""
        deployment_name = deployment["name"]
        resultaat: dict[str, Any] = {
            "service": "seaweedfs",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }
        if not self.project_manager._project_file_handler.project_declares_service(
            project_data, [ServiceType.SEAWEEDFS_STORAGE.value]
        ):
            resultaat["operations"].append(
                {"type": "seaweedfs_cleanup", "status": "skipped", "reason": "project noemt seaweedfs-storage nergens"}
            )
            return resultaat
        if not SeaweedfsConnector.is_configured():
            resultaat["operations"].append(
                {"type": "seaweedfs_cleanup", "status": "skipped", "reason": "cluster levert seaweedfs-storage niet"}
            )
            return resultaat

        project_name = await self.project_manager.get_name()
        gebruiker = generate_minio_username(project_name, deployment_name)
        bucket = generate_bucket_name(project_name, deployment_name)
        policy = generate_minio_policy_name(project_name, deployment_name)

        connector = create_seaweedfs_connector()
        # In deze volgorde: eerst het recht weg, dan de identiteit, dan de data. Elke stap
        # verdraagt "bestaat niet", dus een halve opruiming is af te maken door opnieuw te
        # draaien.
        for wat, naam, handeling in (
            ("seaweedfs_policy_detach", policy, connector.detach_policy(policy, gebruiker)),
            ("seaweedfs_policy", policy, connector.delete_policy(policy)),
            ("seaweedfs_user", gebruiker, connector.delete_user(gebruiker)),
            ("seaweedfs_bucket", bucket, connector.delete_bucket(bucket)),
        ):
            try:
                await handeling
                resultaat["operations"].append({"type": wat, "target": naam, "status": "deleted"})
            except Exception as e:
                resultaat["success"] = False
                resultaat["errors"].append(f"{wat} van {naam} mislukte: {e}")
                logger.exception("Opruimen van %s %s mislukte", wat, naam)
        return resultaat

    async def handle_service_removal(
        self,
        project_name: str,
        deployment_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: MarkedForDeletionService | None = None,
    ) -> dict[str, Any]:
        """De dienst gaat van een deployment af: uitgesteld opruimen, zoals bij minio.

        Met een ``marked_for_deletion_service`` gaan bucket, identiteit en policy op de
        markeringslijst en ruimt de purge in ``opi/jobs/reconciliation.py`` ze op na de
        bewaartermijn. Zonder die dienst (de verwijderflow van een heel project) gaan ze
        direct weg.
        """
        if marked_for_deletion_service is None:
            resultaat = await self.delete_resources_for_deployment(project_data, deployment_data)
            resultaat["trigger"] = "service_removal"
            return resultaat

        cluster = deployment_data.get("cluster", "")
        namen = {
            "seaweedfs_bucket": generate_bucket_name(project_name, deployment_name),
            "seaweedfs_user": generate_minio_username(project_name, deployment_name),
            "seaweedfs_policy": generate_minio_policy_name(project_name, deployment_name),
        }
        for soort in MARKED_RESOURCE_TYPES:
            await marked_for_deletion_service.mark_resource(
                resource_type=soort,
                resource_name=namen[soort],
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
            )
        logger.info(
            "SeaweedFS-resources gemarkeerd voor uitgesteld opruimen: %s (project=%s, deployment=%s)",
            namen["seaweedfs_bucket"],
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
                    "resource_name": namen["seaweedfs_bucket"],
                    "status": "marked",
                }
            ],
            "success": True,
            "errors": [],
        }
