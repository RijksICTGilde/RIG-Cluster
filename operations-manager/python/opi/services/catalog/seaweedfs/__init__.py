"""seaweedfs-storage: de proef met SeaweedFS als tweede object-storage-service (RC-331).

De opvolger van ``minio-storage``: sinds RC-382 draagt die dienst ``replaced_by`` naar deze,
dus op een cluster waar SeaweedFS er is ziet een nieuw project de MinIO-kaart niet meer. Wat
erachter zit is wel anders: een ander beheerprotocol, een ander IAM-model, andere poorten. De
dienst staat alleen op de sandbox, en productie verandert niet.

Drie dingen zijn met opzet zoals ze zijn:

* **Dezelfde variabelen als minio.** ``MinIOVariables`` wordt hergebruikt, zoals
  ``namespace-postgresql-database`` de ``DatabaseVariables`` hergebruikt. Een applicatie
  ziet dus dezelfde namen en wisselt alleen van endpoint. De omschrijvingen noemen nog
  MinIO; neutraal maken raakt de minio-dienst en de API-uitvoer en hoort bij het
  productieplan.
* **Eén van de twee per deployment.** Met dezelfde variabelen botsen de twee diensten in
  één pod, dus ``validate_project`` weigert een deployment die ze beide gebruikt.
* **Wat de proef niet levert, weigert hij.** Geen ``clone-from`` en geen ``backup_label``,
  want kloon en back-up zijn in de gate gemeten maar niet bedraad. Een dienst die aanstaat
  en stil niets doet is wat ``instructions/services.md`` bij approvals verbiedt.

Opruimen is ``IMMEDIATE`` en niet ``DEFERRED`` zoals minio: de uitgestelde weg vraagt een
purge per resourcetype in ``opi/jobs/reconciliation.py``, en dat hoort bij het
productieplan.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from opi.core.cluster_config import get_seaweedfs_host, get_seaweedfs_port
from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.minio.variables import MinIOVariables
from opi.services.catalog.seaweedfs.config_model import SeaweedfsStorageConfig
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceType
from opi.utils.secrets import SeaweedFSSecret

logger = logging.getLogger(__name__)


class SeaweedfsStorageService(Service):
    service_type = ServiceType.SEAWEEDFS_STORAGE
    definition = ServiceDefinition(
        name="SeaweedFS Object Storage (proef)",
        description=(
            "S3-compatibele object storage als proef naast MinIO. Alleen op de sandbox, zonder back-up en zonder kloon."
        ),
        help_template="seaweedfs/help.md",
        icon="map",
        color="groen",
        shared_per_deployment=True,
        secret_class="SeaweedFSSecret",  # noqa: S106  # naam van de sleutel of de secretklasse, geen secret
        variables=[var.value for var in MinIOVariables],
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
    )
    config_model = SeaweedfsStorageConfig
    config_schema_version = "1.0"
    # Mag zichzelf inschrijven, met dezelfde reden als minio: een bucket vraagt geen keuze
    # op projectniveau, en enable-versioning staat standaard uit.
    allows_implicit_project_selection = True
    cleanup_manager_key = ManagerKey.SEAWEEDFS
    #: Direct na minio, zodat geen bestaande orde verschuift.
    provision_order = 21
    manifest_secret_class = SeaweedFSSecret
    manifest_order = 21

    config_section_id: ClassVar[str] = "seaweedfs-config"
    modal_flow_id = "modal-edit-seaweedfs-config"

    def available_on_cluster(self, cluster: str) -> bool:
        """Alleen waar de clusterconfiguratie een SeaweedFS-adres kent.

        Uit de configuratie en niet uit een clusternaam, zoals bij vlam: beide consumenten
        lopen hier langs, de kaart in de wizard en de weigering bij het opslaan.
        """
        return get_seaweedfs_host(cluster) is not None

    def validate_project(self, project_data: dict[str, Any]) -> list[str]:
        """Niet samen met minio op één deployment, en geen kloon.

        Draait voor ELK project, ook een dat deze dienst niet noemt; beide regels geven dan
        niets terug.
        """
        from opi.handlers.project_file_handler import ProjectFileHandler

        # Via de handler en niet via de componentenlijst zelf: die kent ook de
        # helm-charts van een deployment en het v1-sleutelwoord ``uses-services``.
        handler = ProjectFileHandler()
        fouten: list[str] = []
        for deployment in project_data.get("deployments", []) or []:
            naam = deployment.get("name")
            if not naam:
                continue
            if not handler.deployment_uses_service(project_data, naam, [ServiceType.SEAWEEDFS_STORAGE.value]):
                continue
            if handler.deployment_uses_service(project_data, naam, [ServiceType.MINIO_STORAGE.value]):
                fouten.append(
                    f"Deployment '{naam}' gebruikt zowel minio-storage als seaweedfs-storage. Beide diensten "
                    "geven dezelfde OBJECT_STORE_*-variabelen, dus in een pod zou de ene de andere overschrijven. "
                    "Kies er een."
                )
            if deployment.get("clone-from"):
                fouten.append(
                    f"Deployment '{naam}' gebruikt seaweedfs-storage met clone-from. De proef kan geen bucket "
                    "klonen; gebruik minio-storage of haal de clone-from weg."
                )
        return fouten

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        if layer is ConfigLayer.PROJECT:
            return self.config_model_field_names()
        return []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.seaweedfs.editables import SEAWEEDFS_ENABLE_VERSIONING_EDITABLE

        return [SEAWEEDFS_ENABLE_VERSIONING_EDITABLE]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        # Gecached: afnemers vergelijken de identiteit van een sectie (EDIT_SECTIONS[...] is X).
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.base import config_path
            from opi.services.catalog.seaweedfs.visualizers import SEAWEEDFS_ENABLE_VERSIONING

            cached = FormSection(
                section_id=self.config_section_id,
                title="SeaweedFS-objectopslag configuratie",
                icon="wolk",
                description="Instellingen voor de SeaweedFS-bucket van dit project",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[SEAWEEDFS_ENABLE_VERSIONING],
                layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "enable-versioning")],
            )
            self._config_section_cache = cached
        return cached

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        from opi.services.services import service_entry_name

        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.seaweedfs_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, "seaweedfs", SeaweedFSSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' uses SeaweedFS but no object storage credentials found")
            return []
        host = get_seaweedfs_host(ctx.cluster)
        if host is None:
            logger.warning(f"Cluster '{ctx.cluster}' levert seaweedfs-storage niet, geen secret voor deze deployment")
            return []
        # host/poort zijn clusterspecifiek; de rest komt uit de geprovisioneerde sleutels.
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
                # "minio" en niet "seaweedfs": de aliasbucket is gesleuteld op de
                # categorie van _get_service_category_name, waar beide diensten op
                # collapsen omdat ze dezelfde variabelen leveren.
                secret_type="minio",  # noqa: S106  # naam van de sleutel of de secretklasse, geen secret
                resolve_aliases=True,
            )
        ]
