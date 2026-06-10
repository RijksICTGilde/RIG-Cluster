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

LATEST_SCHEMA_VERSION = 2.2

# NOTE: Domain restriction changes (task-1) introduced:
# - domains.allowed-subdomains entries changed from list[str] to list[{name, status, history}]
# - domains.custom-domains renamed to domains.allowed-domains
# No migration needed yet — all existing projects predate the domain restriction feature.
# When migrating existing projects, add a v2.2→v2.3 migration that converts the old formats.

# Storage service types and their corresponding storage type values
_STORAGE_SERVICE_TO_TYPE = {
    ServiceType.PERSISTENT_STORAGE.value: "persistent",
    ServiceType.TEMP_STORAGE.value: "ephemeral",
}

_STORAGE_TYPE_TO_SERVICE = {v: k for k, v in _STORAGE_SERVICE_TO_TYPE.items()}


def detect_schema_version(project_data: dict[str, Any]) -> int | float:
    """
    Detect the schema version of a project file.

    Checks for an explicit `schema-version` field first. Falls back to
    structure detection: presence of `uses-services` on any component,
    helm-chart, helmfile, or deployment indicates v1.
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

    for dep in project_data.get("deployments", []):
        if isinstance(dep, dict) and "uses-services" in dep:
            return 1

    # No version field and no uses-services found - assume latest (v2).
    # Only files with explicit uses-services are v1.
    return LATEST_SCHEMA_VERSION


def migrate_to_latest(project_data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """
    Run all needed migrations to bring project data to the latest schema version.

    Also repairs v2 files that still contain stale ``uses-services`` or
    ``storage`` keys (written by a bug in ``add_services_to_project`` that
    has since been fixed).

    Returns:
        Tuple of (migrated_data, was_migrated). was_migrated is True if any
        migration was applied.
    """
    version = detect_schema_version(project_data)
    migrated = False

    if version < 2:
        project_data = _migrate_v1_to_v2(project_data)
        migrated = True
    else:
        # Repair: clean up stale v1 keys on files already marked v2
        if _cleanup_stale_v1_keys(project_data):
            migrated = True

    if version < 2.1 and _migrate_v2_to_v2_1(project_data):
        migrated = True

    if version < 2.2 and _migrate_v2_1_to_v2_2(project_data):
        migrated = True

    if migrated:
        project_data["schema-version"] = LATEST_SCHEMA_VERSION

    # Always run v2 fixups to clean up corruption from past bugs
    if _fixup_v2_data(project_data):
        migrated = True

    return project_data, migrated


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

    # Migrate deployments
    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict):
            continue
        _merge_uses_services_into_services(dep)

    project_data["schema-version"] = LATEST_SCHEMA_VERSION

    logger.info(f"Migration to v2 complete for project '{project_name}'")
    return project_data


def _migrate_component_v1_to_v2(component: dict[str, Any]) -> None:
    """Migrate a single component from v1 to v2 format, in place."""
    uses_services = component.get("uses-services", [])
    storage_items = component.get("storage", [])

    # Nothing to migrate if there are no v1 keys
    if "uses-services" not in component and "storage" not in component:
        return

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

    # Start from existing v2 services (if any) and merge in v1 data
    existing_services: list[str | dict[str, Any]] = component.get("services", [])
    if not isinstance(existing_services, list):
        existing_services = []

    existing_names: set[str] = set()
    for entry in existing_services:
        if isinstance(entry, str):
            existing_names.add(entry)
        elif isinstance(entry, dict):
            existing_names.update(entry.keys())

    # Build new entries from uses-services, skipping already present ones
    for entry in uses_services:
        if isinstance(entry, str):
            if entry in existing_names:
                continue
            if entry in storage_by_service:
                existing_services.append({entry: {"config": storage_by_service.pop(entry)}})
            else:
                existing_services.append(entry)
        elif isinstance(entry, dict):
            for key in list(entry.keys()):
                if key in existing_names:
                    continue
                if key in storage_by_service:
                    service_dict = entry[key] if isinstance(entry[key], dict) else {}
                    service_dict["config"] = storage_by_service.pop(key)
                    entry[key] = service_dict
            existing_services.append(entry)

    # Remaining storage items not matched to any service
    for service_name, items in storage_by_service.items():
        if service_name not in existing_names:
            existing_services.append({service_name: {"config": items}})

    component["services"] = existing_services

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


def _cleanup_stale_v1_keys(project_data: dict[str, Any]) -> bool:
    """Remove stale ``uses-services`` / ``storage`` keys from v2 project files.

    These can exist when an older version of ``add_services_to_project()``
    wrote v1-style keys back into a file that had already been migrated to v2.

    Because the component may already have a valid ``services`` list (v2),
    we merge any entries from the stale ``uses-services`` into it rather
    than overwriting.

    Returns True if any cleanup was performed.
    """
    cleaned = False

    for component in project_data.get("components", []):
        if not isinstance(component, dict):
            continue
        if "uses-services" not in component and "storage" not in component:
            continue

        project_name = project_data.get("name", "unknown")
        comp_name = component.get("name", "?")
        logger.warning(
            f"Project '{project_name}' component '{comp_name}' has stale v1 keys on a v2 file - merging into services"
        )
        _merge_stale_v1_into_v2(component)
        cleaned = True

    for chart in project_data.get("helm-charts", []):
        if isinstance(chart, dict) and "uses-services" in chart:
            _migrate_uses_services_key(chart)
            cleaned = True

    for hf in project_data.get("helmfile", []):
        if isinstance(hf, dict) and "uses-services" in hf:
            _migrate_uses_services_key(hf)
            cleaned = True

    for dep in project_data.get("deployments", []):
        if isinstance(dep, dict) and "uses-services" in dep:
            _merge_uses_services_into_services(dep)
            cleaned = True

    return cleaned


def _merge_stale_v1_into_v2(component: dict[str, Any]) -> None:
    """Merge stale ``uses-services``/``storage`` into existing v2 ``services``.

    The old ``add_services_to_project()`` wrote ``uses-services`` alongside
    the existing ``services`` key.  This merges those stale entries (with
    any storage config) into the canonical ``services`` list and removes
    the old keys.
    """
    existing_services: list[str | dict[str, Any]] = component.get("services", [])
    stale_services: list[str] = component.get("uses-services", [])
    stale_storage: list[dict[str, Any]] = component.get("storage", [])

    if not isinstance(stale_services, list):
        stale_services = []
    if not isinstance(stale_storage, list):
        stale_storage = []

    # Collect names already present in the v2 services list
    existing_names: set[str] = set()
    for entry in existing_services:
        if isinstance(entry, str):
            existing_names.add(entry)
        elif isinstance(entry, dict):
            existing_names.update(entry.keys())

    # Group stale storage items by service name
    storage_by_service: dict[str, list[dict[str, Any]]] = {}
    for item in stale_storage:
        if not isinstance(item, dict):
            continue
        storage_type = item.get("type", "persistent")
        service_name = _STORAGE_TYPE_TO_SERVICE.get(storage_type)
        if service_name:
            storage_by_service.setdefault(service_name, []).append({k: v for k, v in item.items() if k != "type"})

    # Merge stale services that aren't already in the v2 list
    for svc_name in stale_services:
        if svc_name in existing_names:
            continue
        if svc_name in storage_by_service:
            existing_services.append({svc_name: {"config": storage_by_service[svc_name]}})
        else:
            existing_services.append(svc_name)

    component["services"] = existing_services
    component.pop("uses-services", None)
    component.pop("storage", None)


def _merge_uses_services_into_services(entity: dict[str, Any]) -> None:
    """Safely rename ``uses-services`` → ``services``, merging if both exist.

    Used for deployments, helm-charts, and helmfiles where entries are plain
    strings (no storage config to embed).
    """
    stale: list[str] = entity.get("uses-services", [])
    if not isinstance(stale, list):
        stale = []

    existing: list[str | dict[str, Any]] = entity.get("services", [])
    if not isinstance(existing, list):
        existing = []

    existing_names = set()
    for entry in existing:
        if isinstance(entry, str):
            existing_names.add(entry)
        elif isinstance(entry, dict):
            existing_names.update(entry.keys())

    for svc in stale:
        if svc not in existing_names:
            existing.append(svc)

    entity["services"] = existing
    entity.pop("uses-services", None)


def _migrate_v2_to_v2_1(project_data: dict[str, Any]) -> bool:
    """Migrate root component from component-level ``root: true`` to deployment-level ``root-component``.

    For each deployment, if a component has ``root: true``, set ``root-component``
    on the deployment and remove the ``root`` key from the component.
    If the deployment already has ``root-component``, it takes precedence and
    any stale ``root`` flags on components are removed.

    Returns True if any changes were made.
    """
    migrated = False

    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict):
            continue

        existing_root_component = dep.get("root-component")
        components = dep.get("components", [])

        for comp in components:
            if not isinstance(comp, dict):
                continue

            if comp.get("root") is True:
                if not existing_root_component:
                    # Lift the root flag to the deployment level
                    comp_name = comp.get("reference") or comp.get("name")
                    if comp_name:
                        dep["root-component"] = comp_name
                        existing_root_component = comp_name

                del comp["root"]
                migrated = True
            elif "root" in comp:
                # Clean up root: false
                del comp["root"]
                migrated = True

    return migrated


def _fixup_v2_data(project_data: dict[str, Any]) -> bool:
    """Clean up corruption from past bugs on v2 project files.

    Fixes:
    - Literal ``services{...}`` keys on components (wizard bug wrote path syntax as dict keys)
    - Old flat resource format (``cpu: {request, limit}``, ``memory: "256Mi"``)
    - Stale root-level ``publish-on-web: true`` keys on components
    - Dropped ``configuration`` / ``decrypted_configuration`` blocks on deployments
      (write-only credential copies; nothing consumes them and re-encrypting the
      AGE block on every process run caused ciphertext churn and push conflicts)

    Returns True if any cleanup was performed.
    """
    cleaned = False

    all_entities: list[dict[str, Any]] = [c for c in project_data.get("components", []) if isinstance(c, dict)]
    for dep in project_data.get("deployments", []):
        if isinstance(dep, dict):
            all_entities.extend(c for c in dep.get("components", []) if isinstance(c, dict))
            for stale_config_key in ("configuration", "decrypted_configuration"):
                if stale_config_key in dep:
                    del dep[stale_config_key]
                    cleaned = True

    for entity in all_entities:
        # Remove literal services{...} keys (dead data from wizard bug)
        stale_keys = [k for k in entity if isinstance(k, str) and k.startswith("services{")]
        for key in stale_keys:
            del entity[key]
            cleaned = True

        # Remove stale publish-on-web: true at root level (V0 format)
        if entity.get("publish-on-web") is True:
            del entity["publish-on-web"]
            cleaned = True

        # Migrate old flat resource format
        if _fixup_flat_resources(entity):
            cleaned = True

    # Strip stale root flags left on the project-level component catalog
    if _fixup_catalog_root(project_data):
        cleaned = True

    if cleaned:
        project_name = project_data.get("name", "unknown")
        logger.info(f"Cleaned up stale data in project '{project_name}'")

    return cleaned


def _fixup_catalog_root(project_data: dict[str, Any]) -> bool:
    """Strip stale component-level ``root`` flags from the top-level component catalog.

    The original nice-url feature stored the root marker as ``root: true`` on
    catalog components. The schema later moved this to deployment-level
    ``root-component`` (see ``_migrate_v2_to_v2_1``), but that migration only
    scans deployment component refs, not the project-level catalog, and it is
    version-gated -- so files already stamped at the latest version keep the
    stale catalog key and fail schema validation. This fixup runs
    unconditionally to repair them.

    For any catalog component still marked ``root: true``, the marker is lifted
    to ``root-component`` on every deployment that references it and does not
    already have one, then the ``root`` key is removed. ``root: false`` is
    dropped outright.

    Returns True if any cleanup was performed.
    """
    catalog = [c for c in project_data.get("components", []) if isinstance(c, dict)]
    deployments = [d for d in project_data.get("deployments", []) if isinstance(d, dict)]
    cleaned = False

    for comp in catalog:
        if "root" not in comp:
            continue

        is_root = comp.get("root") is True
        comp_name = comp.get("name")
        del comp["root"]
        cleaned = True

        if is_root and comp_name:
            for dep in deployments:
                if dep.get("root-component"):
                    continue
                refs = {r.get("reference") for r in dep.get("components", []) if isinstance(r, dict)}
                if comp_name in refs:
                    dep["root-component"] = comp_name

    return cleaned


def _fixup_flat_resources(entity: dict[str, Any]) -> bool:
    """Migrate old flat resource format to nested requests/limits structure.

    Old format: ``resources: {cpu: {request: "50m", limit: "1"}, memory: "256Mi"}``
    New format: ``resources: {requests: {cpu: "50m", memory: "256Mi"}, limits: {cpu: "1", memory: "256Mi"}}``

    Returns True if any migration was performed.
    """
    res = entity.get("resources")
    if not isinstance(res, dict):
        return False

    changed = False

    # Migrate flat cpu formats → requests/limits
    cpu = res.get("cpu")
    if isinstance(cpu, dict) and ("request" in cpu or "limit" in cpu):
        # Old dict format: cpu: {request: "50m", limit: "1"}
        if "requests" not in res:
            res["requests"] = {}
        if "limits" not in res:
            res["limits"] = {}
        if "request" in cpu and "cpu" not in res["requests"]:
            res["requests"]["cpu"] = str(cpu["request"])
        if "limit" in cpu and "cpu" not in res["limits"]:
            res["limits"]["cpu"] = str(cpu["limit"])
        del res["cpu"]
        changed = True
    elif isinstance(cpu, str | int | float):
        # Plain value: cpu: "1" → treat as limit (matches how memory shorthand works)
        if "limits" not in res:
            res["limits"] = {}
        if "cpu" not in res["limits"]:
            res["limits"]["cpu"] = str(cpu)
        del res["cpu"]
        changed = True

    # Migrate flat memory: "256Mi" → limits: {memory: "256Mi"}
    memory = res.get("memory")
    if isinstance(memory, str):
        if "limits" not in res:
            res["limits"] = {}
        if "memory" not in res["limits"]:
            res["limits"]["memory"] = memory
        if "requests" not in res:
            res["requests"] = {}
        if "memory" not in res["requests"]:
            res["requests"]["memory"] = memory
        del res["memory"]
        changed = True

    return changed


def _migrate_v2_1_to_v2_2(project_data: dict[str, Any]) -> bool:
    """Normalize component path to list-of-dicts format.

    Converts:
    - ``path: "/foo"`` + ``rewrite-path: "/"`` → ``path: [{match: "/foo", rewrite: "/"}]``
    - ``path: "/foo"`` (no rewrite) → ``path: [{match: "/foo"}]``

    Applies to both top-level components and deployment-level component overrides.

    Returns True if any changes were made.
    """
    migrated = False

    for comp in project_data.get("components", []):
        if not isinstance(comp, dict):
            continue
        if _normalize_path_to_list(comp):
            migrated = True

    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict):
            continue
        for comp in dep.get("components", []):
            if not isinstance(comp, dict):
                continue
            if _normalize_path_to_list(comp):
                migrated = True

    return migrated


def _normalize_path_to_list(entity: dict[str, Any]) -> bool:
    """Convert path string + rewrite-path into a list-of-dicts on a single entity.

    Also renames legacy ``paths`` (plural) to ``path`` (singular).

    Returns True if any changes were made.
    """
    changed = False

    # Rename legacy plural key
    if "paths" in entity and "path" not in entity:
        entity["path"] = entity.pop("paths")
        changed = True
    elif "paths" in entity:
        entity.pop("paths")
        changed = True

    path = entity.get("path")
    rewrite = entity.pop("rewrite-path", None)

    if isinstance(path, str):
        entry: dict[str, str] = {"match": path}
        if rewrite:
            entry["rewrite"] = rewrite
        entity["path"] = [entry]
        return True

    if rewrite and isinstance(path, list):
        # path is already a list but rewrite-path was lingering — just remove it
        return True

    if rewrite and path is None:
        # rewrite-path without path — shouldn't happen, but clean up
        return True

    return changed
