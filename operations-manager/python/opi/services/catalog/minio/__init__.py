"""minio-storage service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from opi.core.cluster_config import get_minio_host, get_minio_port
from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.minio.actions import minio_storage_actions
from opi.services.catalog.minio.config_model import MinioStorageConfig
from opi.services.catalog.minio.variables import MinIOVariables
from opi.services.catalog.shared.backups import BackupsPageMixin
from opi.services.catalog.shared.noncurrent_versions import (
    NONCURRENT_VERSION_SETTINGS,
    noncurrent_deployment_section,
)
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceType
from opi.utils.secrets import MinIOSecret

if TYPE_CHECKING:
    from opi.services.catalog.config_settings import ConfigSetting

logger = logging.getLogger(__name__)


class MinioStorageService(BackupsPageMixin, Service):
    service_type = ServiceType.MINIO_STORAGE
    definition = ServiceDefinition(
        name="MinIO Object Storage",
        description="S3-compatible object storage voor documenten, afbeeldingen en grote bestanden",
        help_template="minio/help.md",
        icon="map",
        color="rood",
        shared_per_deployment=True,
        secret_class="MinIOSecret",  # noqa: S106  # naam van de sleutel of de secretklasse, geen secret
        variables=[var.value for var in MinIOVariables],
        cleanup_strategy=CleanupStrategy.DEFERRED,
        backup_label="minio",
        actions_provider=minio_storage_actions,
    )
    config_model = MinioStorageConfig
    config_schema_version = "1.0"
    # Succeeded by SeaweedFS on clusters where it's available; see ``replaced_on_cluster``.
    replaced_by = ServiceType.SEAWEEDFS_STORAGE
    # May enrol itself (RC-84): a bucket needs no project-level choice. enable-versioning
    # falls back to the platform default and the rest of the model is clone state the
    # platform writes itself.
    allows_implicit_project_selection = True
    cleanup_manager_key = ManagerKey.MINIO
    provision_order = 20
    manifest_secret_class = MinIOSecret
    manifest_order = 20

    config_section_id: ClassVar[str] = "minio-config"
    modal_flow_id = "modal-edit-minio-config"

    def config_settings(self) -> tuple[ConfigSetting, ...]:
        return NONCURRENT_VERSION_SETTINGS

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # minio carries the user setting enable-versioning at project level and OPI-managed
        # clone state (generation/revisions) at deployment level; both are fields of the one
        # union model, so derive the accepted-field hint from it for those layers (checklist 3).
        if layer in (ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT):
            return self.config_model_field_names()
        return []

    def config_editables(self, layer: ConfigLayer):
        if layer is ConfigLayer.PROJECT:
            from opi.services.catalog.minio.editables import MINIO_ENABLE_VERSIONING_EDITABLE
            from opi.services.catalog.minio.visualizers import (
                MAX_NONCURRENT_VERSIONS_FIELD,
                NONCURRENT_EXPIRE_DAYS_FIELD,
            )

            return [
                MINIO_ENABLE_VERSIONING_EDITABLE,
                MAX_NONCURRENT_VERSIONS_FIELD.editable,
                NONCURRENT_EXPIRE_DAYS_FIELD.editable,
            ]
        if layer is ConfigLayer.DEPLOYMENT:
            from opi.services.catalog.minio.visualizers import (
                DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD,
                DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD,
            )

            return [
                DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD.editable,
                DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD.editable,
            ]
        return []

    def config_form_section(self, layer: ConfigLayer):
        if layer is ConfigLayer.DEPLOYMENT:
            return self.deployment_form_section()
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        # Cached: consumers compare section identity (EDIT_SECTIONS[...] is X).
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.layout import Fieldset
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.base import config_path
            from opi.services.catalog.minio.visualizers import (
                MAX_NONCURRENT_VERSIONS_FIELD,
                MINIO_ENABLE_VERSIONING,
                NONCURRENT_EXPIRE_DAYS_FIELD,
            )

            cached = FormSection(
                section_id=self.config_section_id,
                title="Objectopslag configuratie",
                icon="wolk",
                description="Instellingen voor de MinIO-bucket van dit project",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[MINIO_ENABLE_VERSIONING, MAX_NONCURRENT_VERSIONS_FIELD, NONCURRENT_EXPIRE_DAYS_FIELD],
                layout=[
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "enable-versioning"),
                    Fieldset(
                        legend="Oude versies",
                        description=(
                            "Geldt alleen voor een bucket met versiebeheer. Oude versies boven het aantal "
                            "worden opgeruimd, en met een leeftijdsgrens ook de versies die ouder zijn. "
                            "Je kunt dus niet verder terug dan wat hier staat."
                        ),
                        # Het hele blok staat achter het vinkje: wie versiebeheer niet
                        # aanvinkt ziet geen grenzen om in te stellen. De renderer
                        # hertekent live (het vinkje draagt data-rerender).
                        depends_on=config_path(ConfigLayer.PROJECT, self.service_type, "config", "enable-versioning"),
                        children=[
                            MAX_NONCURRENT_VERSIONS_FIELD.editable.yaml_path,
                            NONCURRENT_EXPIRE_DAYS_FIELD.editable.yaml_path,
                        ],
                    ),
                ],
            )
            self._config_section_cache = cached
        return cached

    def deployment_form_section(self, deployment_index: int | None = None):
        """De grens op oude versies voor een bucket; zonder index beschrijft hij de laag."""
        from opi.services.catalog.minio.visualizers import (
            DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD,
            DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD,
        )

        return noncurrent_deployment_section(
            "minio-deployment-config",
            (DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD, DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD),
            deployment_index,
        )

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility, derived from this service's own service_type."""
        from opi.services.services import service_entry_name

        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.minio_manager.create_resources_for_deployment(
            ctx.project_data,
            ctx.deployment,
            ctx.force_clone,
            rotate_secret_key=self.service_type.value in ctx.rotate_credentials,
        )

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
                secret_type="minio",  # noqa: S106  # naam van de sleutel of de secretklasse, geen secret
                resolve_aliases=True,
            )
        ]
