"""namespace-postgresql-database service (dedicated per-namespace CNPG cluster)."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.config_models.namespace_postgres import NamespacePostgresConfig
from opi.services.services_enums import ServiceType


class NamespacePostgresqlDatabaseService(Service):
    service_type = ServiceType.NAMESPACE_POSTGRESQL_DATABASE
    cleanup_manager_key = "database"
    config_model = NamespacePostgresConfig
    config_schema_version = "1.0"
    config_section_id = "postgresql-config"
    modal_flow_id = "modal-edit-postgresql-config"
