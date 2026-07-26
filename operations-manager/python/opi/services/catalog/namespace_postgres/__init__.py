"""namespace-postgresql-database service (dedicated per-namespace CNPG cluster).

Owns its project-level config UI (instances + storage). Note the API surface
(config_api_fields, from the model) is broader than the wizard section: the model has
image/registry/privileges/postInitSQL/resources too, which are API/YAML-only for now.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.base import ConfigLayer, Service, config_path
from opi.services.catalog.namespace_postgres.config_model import NamespacePostgresConfig
from opi.services.services import service_entry_name
from opi.services.services_enums import ServiceType


class NamespacePostgresqlDatabaseService(Service):
    service_type = ServiceType.NAMESPACE_POSTGRESQL_DATABASE
    cleanup_manager_key = "database"
    config_model = NamespacePostgresConfig
    config_schema_version = "1.0"
    config_section_id = "postgresql-config"
    modal_flow_id = "modal-edit-postgresql-config"

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: derived from this service's service_type."""
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # Full config surface, derived from NamespacePostgresConfig (broader than the
        # UI section, which only exposes instances + storage).
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.namespace_postgres.editables import (
            POSTGRESQL_INSTANCES_EDITABLE,
            POSTGRESQL_STORAGE_EDITABLE,
        )

        return [POSTGRESQL_INSTANCES_EDITABLE, POSTGRESQL_STORAGE_EDITABLE]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.namespace_postgres.visualizers import POSTGRESQL_INSTANCES, POSTGRESQL_STORAGE

            cached = FormSection(
                section_id="postgresql-config",
                title="Database configuratie",
                icon="database",
                description="PostgreSQL database-instellingen",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[POSTGRESQL_INSTANCES, POSTGRESQL_STORAGE],
                layout=[
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "instances"),
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "storage"),
                ],
            )
            self._config_section_cache = cached
        return cached
