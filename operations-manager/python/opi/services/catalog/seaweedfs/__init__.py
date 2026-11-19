"""seaweedfs-storage: SeaweedFS as the second object storage service.

The successor of ``minio-storage``: that service carries ``replaced_by`` to this one, so on a
cluster where SeaweedFS exists a new project does not see the MinIO card. What sits behind it
does differ: another management protocol, another IAM model, other ports. The service exists
on every cluster whose configuration names a ``seaweedfs_host``.

Two things are deliberately as they are:

* **The same variables as minio.** ``MinIOVariables`` is reused, the way
  ``namespace-postgresql-database`` reuses ``DatabaseVariables``. An application therefore sees
  the same names and only switches endpoint. The descriptions still say MinIO; making them
  neutral touches the minio service and the API output.
* **One of the two per deployment.** With the same variables the two services collide in one
  pod, so ``validate_project`` refuses a deployment that uses both.

The ``backup_label`` is ``minio`` and not a label of its own: the label groups services with
the same kind of backup, and that is a bucket behind an S3 API here. A label of its own would
double the backup tab, the restore route, the resource type of the backup API and the UI list,
all for the same action.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from opi.core.cluster_config import get_seaweedfs_host, get_seaweedfs_port
from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.minio.variables import MinIOVariables
from opi.services.catalog.seaweedfs.actions import seaweedfs_storage_actions
from opi.services.catalog.seaweedfs.config_model import SeaweedfsStorageConfig
from opi.services.catalog.shared.backups import BackupsPageMixin
from opi.services.catalog.shared.noncurrent_versions import (
    NONCURRENT_VERSION_SETTINGS,
    noncurrent_deployment_section,
)
from opi.services.secrets import SeaweedFSSecret
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceType

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection
    from opi.services.catalog.config_settings import ConfigSetting

logger = logging.getLogger(__name__)


class SeaweedfsStorageService(BackupsPageMixin, Service):
    service_type = ServiceType.SEAWEEDFS_STORAGE
    definition = ServiceDefinition(
        name="SeaweedFS Object Storage",
        description="S3-compatibele object storage voor documenten, afbeeldingen en grote bestanden",
        help_template="seaweedfs/help.md",
        icon="map",
        color="groen",
        shared_per_deployment=True,
        secret_class="SeaweedFSSecret",  # noqa: S106  # the name of the key or the secret class, not a secret
        variables=[var.value for var in MinIOVariables],
        cleanup_strategy=CleanupStrategy.DEFERRED,
        backup_label="minio",
        actions_provider=seaweedfs_storage_actions,
    )
    config_model = SeaweedfsStorageConfig
    config_schema_version = "1.0"
    # May enrol itself, for the same reason as minio: a bucket asks for no project-level
    # choice, and enable-versioning is off by default.
    allows_implicit_project_selection = True
    cleanup_manager_key = ManagerKey.SEAWEEDFS
    #: Right after minio.
    provision_order = 21
    manifest_secret_class = SeaweedFSSecret
    manifest_order = 21

    config_section_id: ClassVar[str] = "seaweedfs-config"
    modal_flow_id = "modal-edit-seaweedfs-config"

    def config_settings(self) -> tuple[ConfigSetting, ...]:
        return NONCURRENT_VERSION_SETTINGS

    def available_on_cluster(self, cluster: str) -> bool:
        """Only where the cluster configuration knows a SeaweedFS address.

        From the configuration and not from a cluster name, as with vlam: both consumers come
        past here, the card in the wizard and the refusal on save.
        """
        return get_seaweedfs_host(cluster) is not None

    def validate_project(self, project_data: dict[str, Any]) -> list[str]:
        """Not together with minio on one deployment.

        Runs for EVERY project, including one that does not name this service; the rule then
        returns nothing.
        """
        from opi.services.project_file_handler import ProjectFileHandler

        # Through the handler and not through the component list itself: the handler also knows
        # a deployment's helm charts and the v1 keyword ``uses-services``.
        handler = ProjectFileHandler()
        errors: list[str] = []
        for deployment in project_data.get("deployments", []) or []:
            name = deployment.get("name")
            if not name:
                continue
            if not handler.deployment_uses_service(project_data, name, [ServiceType.SEAWEEDFS_STORAGE.value]):
                continue
            if handler.deployment_uses_service(project_data, name, [ServiceType.MINIO_STORAGE.value]):
                errors.append(
                    f"Deployment '{name}' gebruikt zowel minio-storage als seaweedfs-storage. Beide diensten "
                    "geven dezelfde OBJECT_STORE_*-variabelen, dus in een pod zou de ene de andere overschrijven. "
                    "Kies er een."
                )
        return errors

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # The limit on noncurrent versions sits on both layers, and both are fields of the one
        # model, so the list comes from there.
        if layer in (ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT):
            return self.config_model_field_names()
        return []

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        if layer is ConfigLayer.PROJECT:
            from opi.services.catalog.seaweedfs.editables import SEAWEEDFS_ENABLE_VERSIONING_EDITABLE
            from opi.services.catalog.seaweedfs.visualizers import (
                MAX_NONCURRENT_VERSIONS_FIELD,
                NONCURRENT_EXPIRE_DAYS_FIELD,
            )

            return [
                SEAWEEDFS_ENABLE_VERSIONING_EDITABLE,
                MAX_NONCURRENT_VERSIONS_FIELD.editable,
                NONCURRENT_EXPIRE_DAYS_FIELD.editable,
            ]
        if layer is ConfigLayer.DEPLOYMENT:
            from opi.services.catalog.seaweedfs.visualizers import (
                DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD,
                DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD,
            )

            return [
                DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD.editable,
                DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD.editable,
            ]
        return []

    def deployment_form_section(self, deployment_index: int | None = None) -> FormSection:
        """The limit on noncurrent versions for a bucket; without an index it describes the layer."""
        from opi.services.catalog.seaweedfs.visualizers import (
            DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD,
            DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD,
        )

        return noncurrent_deployment_section(
            "seaweedfs-deployment-config",
            (DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD, DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD),
            deployment_index,
        )

    def config_form_section(self, layer: ConfigLayer) -> FormSection | None:
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
            from opi.services.catalog.seaweedfs.visualizers import (
                MAX_NONCURRENT_VERSIONS_FIELD,
                NONCURRENT_EXPIRE_DAYS_FIELD,
                SEAWEEDFS_ENABLE_VERSIONING,
            )

            cached = FormSection(
                section_id=self.config_section_id,
                title="SeaweedFS-objectopslag configuratie",
                icon="wolk",
                description="Instellingen voor de SeaweedFS-bucket van dit project",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[SEAWEEDFS_ENABLE_VERSIONING, MAX_NONCURRENT_VERSIONS_FIELD, NONCURRENT_EXPIRE_DAYS_FIELD],
                layout=[
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "enable-versioning"),
                    Fieldset(
                        legend="Versiebeheer voor objectstorage",
                        description=(
                            "Geldt alleen voor een bucket met versiebeheer. Oude versies boven het aantal "
                            "worden opgeruimd, en met een leeftijdsgrens ook de versies die ouder zijn. "
                            "Je kunt dus niet verder terug dan wat hier staat."
                        ),
                        # The whole block sits behind the checkbox: whoever does not tick
                        # versioning sees no limits to set. The renderer redraws live (the
                        # checkbox carries data-rerender).
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

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        from opi.services.services import service_entry_name

        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.seaweedfs_manager.create_resources_for_deployment(
            ctx.project_data, ctx.deployment, force_clone=ctx.force_clone
        )

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, "seaweedfs", SeaweedFSSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' uses SeaweedFS but no object storage credentials found")
            return []
        host = get_seaweedfs_host(ctx.cluster)
        if host is None:
            logger.warning(f"Cluster '{ctx.cluster}' levert seaweedfs-storage niet, geen secret voor deze deployment")
            return []
        # host/port are cluster specific; the rest comes from the provisioned keys.
        secret = SeaweedFSSecret(
            host=host,
            port=get_seaweedfs_port(ctx.cluster),
            access_key=creds.access_key,
            secret_key=creds.secret_key,
            bucket_name=creds.bucket_name,
            region=creds.region,
        )
        return [
            SecretFileSpec(
                secret_name=SeaweedFSSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=secret.to_k8s_secret_data(),
                # "minio" and not "seaweedfs": the alias bucket is keyed on the category of
                # get_service_category_name, which both services collapse onto because they
                # provide the same variables.
                secret_type="minio",  # noqa: S106  # the name of the key or the secret class, not a secret
                resolve_aliases=True,
            )
        ]
