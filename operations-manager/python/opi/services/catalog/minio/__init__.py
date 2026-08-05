"""minio-storage service."""

from __future__ import annotations

import logging

from opi.core.cluster_config import get_minio_host, get_minio_port
from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.minio.config_model import MinioStorageConfig
from opi.services.catalog.shared.backups import BackupsPageMixin
from opi.services.services_enums import ManagerKey, ServiceType
from opi.utils.secrets import MinIOSecret

logger = logging.getLogger(__name__)


class MinioStorageService(BackupsPageMixin, Service):
    service_type = ServiceType.MINIO_STORAGE
    config_model = MinioStorageConfig
    config_schema_version = "1.0"
    cleanup_manager_key = ManagerKey.MINIO
    provision_order = 20
    manifest_secret_class = MinIOSecret
    manifest_order = 20

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # minio carries the user setting enable-versioning at project level and OPI-managed
        # clone state (generation/revisions) at deployment level; both are fields of the one
        # union model, so derive the accepted-field hint from it for those layers (checklist 3).
        if layer in (ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT):
            return self.config_model_field_names()
        return []

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.minio_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment, ctx.force_clone)

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, "minio", MinIOSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' uses MinIO but no object storage credentials found")
            return []
        # host/port are cluster-specific; the rest comes from the provisioned creds.
        secret = MinIOSecret(
            host=get_minio_host(ctx.cluster),
            port=get_minio_port(ctx.cluster),
            access_key=creds.access_key,
            secret_key=creds.secret_key,
            bucket_name=creds.bucket_name,
            region=creds.region,
        )
        return [
            SecretFileSpec(
                secret_name=MinIOSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=secret.to_k8s_secret_data(),
                secret_type="minio",
                resolve_aliases=True,
            )
        ]
