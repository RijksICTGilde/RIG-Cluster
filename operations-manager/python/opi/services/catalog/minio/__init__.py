"""minio-storage service."""

from __future__ import annotations

import logging
from typing import Any

from opi.core.cluster_config import get_minio_host, get_minio_port
from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.minio.config_model import MinioStorageConfig
from opi.services.services_enums import ManagerKey, ServiceType
from opi.utils.secrets import MinIOSecret

logger = logging.getLogger(__name__)


class MinioStorageService(Service):
    service_type = ServiceType.MINIO_STORAGE
    config_model = MinioStorageConfig
    config_schema_version = "1.0"
    cleanup_manager_key = ManagerKey.MINIO
    provision_order = 20
    manifest_secret_class = MinIOSecret
    manifest_order = 20

    config_section_id = "minio-config"
    modal_flow_id = "modal-edit-minio-config"
    form_exempt_layers = {
        ConfigLayer.DEPLOYMENT: ("clone state (generation/revisions) written by revision_manager, not by a user")
    }

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # minio carries the user setting enable-versioning at project level and OPI-managed
        # clone state (generation/revisions) at deployment level; both are fields of the one
        # union model, so derive the accepted-field hint from it for those layers (checklist 3).
        if layer in (ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT):
            return self.config_model_field_names()
        return []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.minio.editables import MINIO_ENABLE_VERSIONING_EDITABLE

        return [MINIO_ENABLE_VERSIONING_EDITABLE]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        # Cached: consumers compare section identity (EDIT_SECTIONS[...] is X).
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.base import config_path
            from opi.services.catalog.minio.visualizers import MINIO_ENABLE_VERSIONING

            cached = FormSection(
                section_id=self.config_section_id,
                title="Objectopslag configuratie",
                icon="wolk",
                description="Instellingen voor de MinIO-bucket van dit project",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[MINIO_ENABLE_VERSIONING],
                layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "enable-versioning")],
            )
            self._config_section_cache = cached
        return cached

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility, derived from this service's own service_type."""
        from opi.services.services import service_entry_name

        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

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
