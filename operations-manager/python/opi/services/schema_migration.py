"""
Schema migration framework for project YAML files.

Auto-detects the schema version of a project file and migrates it to the
latest version. Structure detection bootstraps the first migration (v1→v2)
and introduces a `schema-version` field; future migrations use the version
number directly.
"""

import logging
from typing import Any

from opi.services.services_enums import ServiceType

logger = logging.getLogger(__name__)

LATEST_SCHEMA_VERSION = 2

# Storage service types and their corresponding storage type values
_STORAGE_SERVICE_TO_TYPE = {
    ServiceType.PERSISTENT_STORAGE.value: "persistent",
    ServiceType.TEMP_STORAGE.value: "ephemeral",
}

_STORAGE_TYPE_TO_SERVICE = {v: k for k, v in _STORAGE_SERVICE_TO_TYPE.items()}


def detect_schema_version(project_data: dict[str, Any]) -> int:
    """
    Detect the schema version of a project file.

    Checks for an explicit `schema-version` field first. Falls back to
    structure detection: presence of `uses-services` on any component,
    helm-chart, or helmfile indicates v1.
    """
    if "schema-version" in project_data:
        return project_data["schema-version"]

    # Structure detection for v1: check for uses-services on any entity
    for comp in project_data.get("components", []):
        if isinstance(comp, dict) and "uses-services" in comp:
            return 1

    for chart in project_data.get("helm-charts", []):
        if isinstance(chart, dict) and "uses-services" in chart:
            return 1

    for hf in project_data.get("helmfile", []):
        if isinstance(hf, dict) and "uses-services" in hf:
            return 1

    # No version field and no uses-services found — assume v1 (unversioned)
    return 1


def migrate_to_latest(project_data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """
    Run all needed migrations to bring project data to the latest schema version.

    Returns:
        Tuple of (migrated_data, was_migrated). was_migrated is True if any
        migration was applied.
    """
    version = detect_schema_version(project_data)

    if version >= LATEST_SCHEMA_VERSION:
        return project_data, False

    if version < 2:
        project_data = _migrate_v1_to_v2(project_data)

    return project_data, True


def _migrate_v1_to_v2(project_data: dict[str, Any]) -> dict[str, Any]:
    """
    Migrate from v1 (uses-services + separate storage block) to v2
    (services with direct names + storage under service config).

    Changes:
    - components: uses-services → services (mixed string/dict format)
    - components: storage block merged into persistent-storage/temp-storage config
    - components: storage type field dropped (implied by service name)
    - helm-charts: uses-services → services
    - helmfiles: uses-services → services
    - schema-version: 2 added at root
    """
    project_name = project_data.get("name", "unknown")
    logger.info(f"Migrating project '{project_name}' from schema v1 to v2")

    # Migrate components
    for component in project_data.get("components", []):
        if not isinstance(component, dict):
            continue
        _migrate_component_v1_to_v2(component)

    # Migrate helm-charts
    for chart in project_data.get("helm-charts", []):
        if not isinstance(chart, dict):
            continue
        _migrate_uses_services_key(chart)

    # Migrate helmfiles
    for hf in project_data.get("helmfile", []):
        if not isinstance(hf, dict):
            continue
        _migrate_uses_services_key(hf)

    project_data["schema-version"] = LATEST_SCHEMA_VERSION

    logger.info(f"Migration to v2 complete for project '{project_name}'")
    return project_data


def _migrate_component_v1_to_v2(component: dict[str, Any]) -> None:
    """Migrate a single component from v1 to v2 format, in place."""
    uses_services = component.get("uses-services", [])
    storage_items = component.get("storage", [])

    if not isinstance(uses_services, list):
        uses_services = []
    if not isinstance(storage_items, list):
        storage_items = []

    # Group storage items by their target service
    storage_by_service: dict[str, list[dict[str, Any]]] = {}
    for item in storage_items:
        if not isinstance(item, dict):
            continue
        storage_type = item.get("type", "persistent")
        service_name = _STORAGE_TYPE_TO_SERVICE.get(storage_type)
        if service_name:
            storage_by_service.setdefault(service_name, []).append({k: v for k, v in item.items() if k != "type"})

    # Build new services list
    new_services: list[str | dict[str, Any]] = []
    for entry in uses_services:
        if isinstance(entry, str):
            # Check if this service has storage items to attach
            if entry in storage_by_service:
                new_services.append({entry: {"config": storage_by_service.pop(entry)}})
            else:
                new_services.append(entry)
        elif isinstance(entry, dict):
            # Dict entry (e.g., authorization-wall: {config: ...})
            # Check if any key matches a storage service
            for key in list(entry.keys()):
                if key in storage_by_service:
                    service_dict = entry[key] if isinstance(entry[key], dict) else {}
                    service_dict["config"] = storage_by_service.pop(key)
                    entry[key] = service_dict
            new_services.append(entry)

    # If there are storage items for services not in uses-services (shouldn't
    # happen normally, but handle gracefully), add them
    for service_name, items in storage_by_service.items():
        new_services.append({service_name: {"config": items}})

    component["services"] = new_services

    # Remove old keys
    component.pop("uses-services", None)
    component.pop("storage", None)


def _migrate_uses_services_key(entity: dict[str, Any]) -> None:
    """Rename uses-services → services for helm-charts and helmfiles."""
    if "uses-services" not in entity:
        return

    uses_services = entity.get("uses-services", [])
    if not isinstance(uses_services, list):
        uses_services = []

    entity["services"] = uses_services
    del entity["uses-services"]
