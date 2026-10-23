"""Manager voor de object-opslag van de proef seaweedfs-storage (RC-331).

Veel kleiner dan ``MinioManager``: geen kloon, geen back-up, geen generaties en geen
uitgesteld opruimen. Wat die manager extra doet hoort bij het productieplan; wat hier staat
is wat de proef levert, een bucket en een identiteit per deployment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.connectors.seaweedfs_shell import SeaweedfsConnector, create_seaweedfs_connector
from opi.core.cluster_config import get_prefixed_namespace, get_seaweedfs_host, get_seaweedfs_port
from opi.core.config import settings
from opi.services import ServiceType
from opi.services.services import service_entry_config, service_entry_name
from opi.utils.naming import generate_bucket_name, generate_minio_policy_name, generate_minio_username
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import SeaweedFSSecret

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

logger = logging.getLogger(__name__)


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

    async def _bestaande_sleutels(self, deployment: dict[str, Any]) -> SeaweedFSSecret | None:
        """De sleutels uit het k8s-secret, als die er zijn.

        ``BaseSecret.get_data`` is de generieke vorm van wat ``MinioManager`` met een eigen
        private helper doet, dus die wordt hier hergebruikt.
        """
        namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])
        return await SeaweedFSSecret.get_data(self.project_manager._kubectl_connector, namespace, deployment["name"])

    async def create_resources_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        """Bucket, identiteit, policy en versioning voor een deployment met deze dienst."""
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

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """Policy, identiteit en bucket weg. Direct, want de proef bewaart geen data."""
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
        """De dienst gaat van een deployment af: direct opruimen.

        ``marked_for_deletion_service`` wordt genegeerd, en dat is de keuze van de proef:
        uitgesteld opruimen vraagt een purge per resourcetype in
        ``opi/jobs/reconciliation.py`` en dat hoort bij het productieplan. De help-tekst en
        het feature-doc zeggen dat het opruimen direct is.
        """
        resultaat = await self.delete_resources_for_deployment(project_data, deployment_data)
        resultaat["trigger"] = "service_removal"
        return resultaat
