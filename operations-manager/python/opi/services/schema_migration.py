"""
Schema migration framework for project YAML files.

Auto-detects the schema version of a project file and migrates it to the
latest version. Structure detection bootstraps the first migration (v1→v2)
and introduces a `schema-version` field; future migrations use the version
number directly.
"""

import logging
from typing import TYPE_CHECKING, Any

from opi.services.postgres_scope import database_generation_service_type
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType
from opi.utils.naming import generate_storage_name

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Every schema version a project file can be stamped with, oldest first. This is
#: the chain, and it is also the contract with ``opi.core.project_schema``: each of
#: these versions must be validatable, so each one except the newest needs a legacy
#: patch in ``opi/schemas/project_legacy/``. ``check_schema_versions`` enforces that
#: at startup, so adding a migration without a schema fails loudly instead of
#: quietly rejecting files that declare the new version.
SCHEMA_VERSIONS: tuple[int | float, ...] = (1, 2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7)

LATEST_SCHEMA_VERSION = SCHEMA_VERSIONS[-1]

# NOTE: Domain restriction changes (task-1) introduced:
# - domains.allowed-subdomains entries changed from list[str] to list[{name, status, history}]
# - domains.custom-domains renamed to domains.allowed-domains
# No migration needed yet — all existing projects predate the domain restriction feature.
# When migrating existing projects, add a v2.2→v2.3 migration that converts the old formats.
#
# v2.4 -> v2.5 (RC-5): the domain-approval block moved from the project root (`domains:`)
# to the publish-on-web service config (`services/[publish-on-web]/config/domains`).
# See ``normalize_domains_location`` below.
#
# v2.5 -> v2.6 (RC-13): the top-level `invites:` block moved to the invite service config
# (`services/invite/config`), its `settings.default_language` flattened to `default-language`
# next to `active`, and its keys hyphenated to match the service model. See
# ``relocate_invites_to_service`` below.
#
# v2.6 -> v2.7 (RC-60): the seven web-address settings moved from the deployment root
# (`base-domain`, `subdomain`, `domain-mode`, `domain-format`, `issuer`, `root-component`,
# `expose-component-on-bare-domain`) to the publish-on-web service config on that deployment
# (`deployments[*]/services{publish-on-web}/config`). See ``relocate_domain_settings_to_service``
# below; the placement itself is decided by ``catalog/publish_on_web/domain_config.py``.

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

    for step_version, step in MIGRATION_STEPS:
        if version < step_version and step(project_data):
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
        if not service_name:
            continue
        entry = {k: v for k, v in item.items() if k != "type"}
        bucket = storage_by_service.setdefault(service_name, [])
        # The v2 storage config (StorageEntry) requires a name per mount, but v1
        # entries often carried none. Synthesize the same name the renderer derives
        # from the mount path (generate_storage_name) so a migrated legacy project
        # stays valid under the config-validation gate instead of failing on the
        # required `name` field.
        if not entry.get("name"):
            entry["name"] = generate_storage_name(entry.get("mount-path", ""), len(bucket))
        bucket.append(entry)

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

        # Rename component-level attachments coupling key use -> config (consistency
        # with other services' component-level "config"; the project-level catalog
        # stays "data"). Keeps existing projects working after the rename.
        services = entity.get("services")
        if isinstance(services, list):
            for svc in services:
                if isinstance(svc, dict) and isinstance(svc.get("attachments"), dict):
                    att = svc["attachments"]
                    if "use" in att and "config" not in att:
                        att["config"] = att.pop("use")
                        cleaned = True

    # Strip stale root flags left on the project-level component catalog
    if _fixup_catalog_root(project_data):
        cleaned = True

    # Relocate a stray top-level `invites:` even on a file already stamped at the latest
    # version (e.g. written by an old pod mid-rollout). The version-gated step above skips
    # such a file; this unconditional fixup repairs it, exactly like _fixup_catalog_root.
    if relocate_invites_to_service(project_data):
        cleaned = True

    if _fixup_duplicate_service_entries(project_data):
        cleaned = True

    if relocate_resource_generations_to_deployment(project_data):
        cleaned = True

    if cleaned:
        project_name = project_data.get("name", "unknown")
        logger.info(f"Cleaned up stale data in project '{project_name}'")

    return cleaned


#: The two service names that can carry the SAME database generation. A project declares one
#: of them, but the old component-level writer always used the fixed ``postgresql-database``
#: key regardless, so a value can sit under either name and both describe the one database of
#: the deployment. They are therefore merged into a single value under the name the project
#: declares (``database_generation_service_type``).
_POSTGRES_GENERATION_SERVICES = (
    ServiceType.POSTGRESQL_DATABASE.value,
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
)


def _generation_groups(project_data: dict[str, Any]) -> list[tuple[tuple[str, ...], str]]:
    """``(service names that may carry the value, name it belongs under)`` per resource.

    Only resources whose generation belongs to the DEPLOYMENT, because the name they
    produce is one per deployment (``{project}_{deployment}_v{gen}`` /
    ``{project}-{deployment}-v{gen}``). ``persistent-storage`` is deliberately absent: a
    PVC name carries the component, so its generation stays component-level.
    """
    return [
        (_POSTGRES_GENERATION_SERVICES, database_generation_service_type(project_data)),
        ((ServiceType.MINIO_STORAGE.value,), ServiceType.MINIO_STORAGE.value),
    ]


def _take_component_generations(component: dict[str, Any], service_type: str) -> list[int]:
    """Pull every generation this component recorded for ``service_type``, removing them.

    Only the shape the buggy writer produced is understood:
    ``component["services"][service_type] = [{"reference": ..., "config": {"generation": N}}]``.
    Empties left behind (config, entry, list, services) are cleaned up so the file does not
    keep a hollow record of a value that has moved.
    """
    services = component.get("services")
    if not isinstance(services, dict):
        return []
    entries = services.get(service_type)
    if not isinstance(entries, list):
        return []

    found: list[int] = []
    emptied: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        config = entry.get("config")
        if not isinstance(config, dict) or config.get("generation") is None:
            continue
        found.append(int(config.pop("generation")))
        if not config:
            del entry["config"]
        emptied.add(id(entry))

    if not found:
        return []

    # Only entries this function hollowed out are dropped. An entry that was already a bare
    # ``{"reference": x}`` before the move says something the relocation was not asked about.
    remaining = [entry for entry in entries if not (id(entry) in emptied and set(entry) <= {"reference"})]
    if remaining:
        services[service_type] = remaining
    else:
        del services[service_type]
    if not services:
        del component["services"]
    return found


def relocate_resource_generations_to_deployment(project_data: dict[str, Any]) -> bool:
    """Move a database/bucket generation from the component up to the deployment (RC-123).

    A database and a bucket are named after the project and the deployment only, so their
    generation describes the deployment. Two write paths disagreed about that: the restore
    router wrote it deployment-level while the async restore task wrote it component-level,
    and the restore read it back component-level -- so a restore saw generation 0 every time,
    computed the SAME target database name twice, and the second restore dumped the backup
    into the live database on top of the rows already there.

    This is a repair, not a schema change: both placements are shape-valid YAML, the value is
    simply in the wrong one. It therefore runs unconditionally on every load (like
    ``_fixup_catalog_root``) rather than behind a version gate, so a file written by an older
    pod mid-rollout is repaired too, and it is idempotent.

    The database value has a second twist: the old component-level writer always used the
    fixed ``postgresql-database`` key, even for a project that declares
    ``namespace-postgresql-database``. Moving it up under the key it was FOUND under would
    leave it where nobody reads it (``get_database_generation`` reads the declared name) and,
    worse, put a shadow entry next to the real one that ``reconciliation`` reads first --
    marking the running ``_vN`` database an orphan. So both PostgreSQL names are merged into
    one value under the name the project declares, and the other name loses its generation.

    Conflict handling is deliberately explicit. When both placements carry a value and they
    disagree, the HIGHER one wins and the choice is logged at warning level with both numbers.
    Higher is the only safe direction: a generation lower than reality resolves to a database
    name that already exists, which is precisely the collision this task exists to stop. The
    restore refuses to write into a non-empty target anyway, so a wrong guess here cannot
    destroy data on its own.

    Returns True if any deployment changed.
    """
    changed = False
    project_name = project_data.get("name", "unknown")

    for deployment in project_data.get("deployments") or []:
        if not isinstance(deployment, dict):
            continue
        deployment_name = deployment.get("name", "unknown")
        components = [c for c in deployment.get("components") or [] if isinstance(c, dict)]

        for service_names, target_type in _generation_groups(project_data):
            component_generations: list[int] = []
            for service_type in service_names:
                for component in components:
                    component_generations.extend(_take_component_generations(component, service_type))

            on_deployment: dict[str, int] = {}
            for service_type in service_names:
                found = _read_deployment_generation(deployment, service_type)
                if found is not None:
                    on_deployment[service_type] = found

            # Names other than the declared one hold a value nobody reads back, and next to a
            # real entry they shadow it for reconciliation. They are merged in and cleared.
            shadow_names = [name for name in on_deployment if name != target_type]
            if not component_generations and not shadow_names:
                continue

            changed = True
            sources = [f"component-level {value}" for value in sorted(set(component_generations))]
            sources += [f"deployment-level {value} (under {name})" for name, value in sorted(on_deployment.items())]
            winner = max(component_generations + list(on_deployment.values()))

            if len({*component_generations, *on_deployment.values()}) > 1:
                logger.warning(
                    f"Conflicting {target_type} generations for deployment '{deployment_name}' in project "
                    f"'{project_name}': {', '.join(sources)}. Keeping {winner}, because a lower generation "
                    f"names a resource that already exists."
                )
            else:
                logger.info(
                    f"Moved {target_type} generation {winner} to deployment '{deployment_name}' "
                    f"in project '{project_name}' ({', '.join(sources)})"
                )

            for name in shadow_names:
                _clear_deployment_generation(deployment, name)
            _write_deployment_generation(deployment, target_type, winner)

    return changed


def _read_deployment_generation(deployment: dict[str, Any], service_type: str) -> int | None:
    """The generation on a deployment's own services entry, or None."""
    for entry in deployment.get("services") or []:
        if service_entry_name(entry) != service_type:
            continue
        if not isinstance(entry, dict):
            # A bare string is the service without any config: the entry for this service
            # has been found and it carries no generation.
            return None
        config = entry.get("config")
        if isinstance(config, dict) and config.get("generation") is not None:
            return int(config["generation"])
        return None
    return None


def _clear_deployment_generation(deployment: dict[str, Any], service_type: str) -> None:
    """Drop the generation from a deployment's services entry, leaving the entry itself.

    The entry may name a service the project really declares, so only the value that has
    moved elsewhere is removed -- an empty ``config`` with it, so no hollow record stays.
    """
    for entry in deployment.get("services") or []:
        if service_entry_name(entry) != service_type or not isinstance(entry, dict):
            continue
        config = entry.get("config")
        if not isinstance(config, dict):
            return
        config.pop("generation", None)
        if not config:
            del entry["config"]
        return


def _write_deployment_generation(deployment: dict[str, Any], service_type: str, generation: int) -> None:
    """Set the generation on a deployment's own services entry, creating what is missing."""
    services = deployment.get("services")
    if not isinstance(services, list):
        services = []
        deployment["services"] = services

    for index, entry in enumerate(services):
        if service_entry_name(entry) != service_type:
            continue
        record: dict[str, Any] = entry if isinstance(entry, dict) else {"reference": service_type}
        services[index] = record
        config = record.get("config")
        if not isinstance(config, dict):
            config = {}
            record["config"] = config
        config["generation"] = generation
        return

    services.append({"reference": service_type, "config": {"generation": generation}})


def _fixup_duplicate_service_entries(project_data: dict[str, Any]) -> bool:
    """Collapse a service named twice in the project-level services list.

    Some past write path appended a service instead of reusing the entry that was
    already there, leaving files with a literal ``["publish-on-web", "publish-on-web"]``.
    ``_validate_services_listed_once`` rejects that, and rightly so, but the effect on
    an existing file is that every reprocess fails silently: no deploys, no auto-tune,
    no error anyone sees. That is the dp-bn7 failure mode, and two production files sit
    in it today (``dsm1j2-2ws`` and ``ug-zxt``, both two bare strings).

    Only collapses when the repeat is unambiguous, meaning at most one of the entries
    carries a non-empty config. Then the duplicate says nothing the survivor does not
    already say and dropping it cannot lose data. Two entries that both carry config
    could contradict each other, so those are left alone for the validator to reject
    rather than silently picking a winner.
    """
    services = project_data.get("services")
    if not isinstance(services, list):
        return False

    by_name: dict[str, list[int]] = {}
    for index, entry in enumerate(services):
        name = service_entry_name(entry)
        if name is not None:
            by_name.setdefault(name, []).append(index)

    drop: set[int] = set()
    for name, indexes in by_name.items():
        if len(indexes) < 2:
            continue
        with_config = [i for i in indexes if service_entry_config(services[i])]
        if len(with_config) > 1:
            logger.warning(
                f"Project '{project_data.get('name', 'unknown')}': service '{name}' staat meerdere keren "
                f"in de services-lijst en meer dan een daarvan draagt config; niet automatisch samengevoegd"
            )
            continue
        # Keep the one carrying config, or else the first occurrence.
        keep = with_config[0] if with_config else indexes[0]
        drop.update(i for i in indexes if i != keep)

    if not drop:
        return False

    project_data["services"] = [entry for i, entry in enumerate(services) if i not in drop]
    logger.info(f"Project '{project_data.get('name', 'unknown')}': {len(drop)} dubbele service-entry(s) samengevoegd")
    return True


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


def _normalize_service_entry(entry: Any, id_key: str) -> Any:
    """Convert a legacy name-as-key service entry to the uniform record form.

    ``{X: {config: ...}}`` -> ``{id_key: X, config: ...}``. Bare strings and entries
    already in record form (they carry ``name``/``reference``) are returned as-is.
    ``id_key`` is ``"name"`` for project-level definitions, ``"reference"`` for
    component-level references.
    """
    if not isinstance(entry, dict):
        return entry
    if "name" in entry or "reference" in entry:
        return entry
    keys = [key for key in entry if key not in ("config", "schema-version")]
    if len(keys) != 1:
        return entry
    name = keys[0]
    if name == "attachments":
        # Deferred hard case: attachments has its own project-level 'data' catalog and
        # dedicated $defs. Leave it in the legacy name-as-key form (readers handle it).
        return entry
    body = entry[name]
    record: dict[str, Any] = {id_key: name}
    if isinstance(body, dict):
        if "config" in body:
            record.update(body)  # config-wrapped legacy (+ any siblings like 'type')
        else:
            # Inline config with no wrapper (e.g. metrics-scraper {port, path}).
            record["config"] = body
    elif body is not None:
        record["config"] = body
    return record


def normalize_service_entries(project_data: dict[str, Any]) -> bool:
    """Normalize service entries to the uniform record form (RC-5 A):
    project-level definitions -> ``{name, config}``, component-level references ->
    ``{reference, config}``. Bare strings stay bare; already-normalized entries and
    attachments (deferred) are untouched. Deployment-level entries normalize to
    ``{reference, config}`` too: they used to be written only by OPI itself (clone state,
    already in that shape), but the cross-domain per-deployment patch form writes them
    through the editables, which produce the legacy name-as-key shape -- and the schema's
    ``deployment-service`` envelope rejects that shape outright.

    Idempotent and version-independent. This is both the v2.3 -> v2.4 migration step
    AND the canonical shape used on the create/wizard save path, so newly created
    project files are born in the current uniform form (the wizard editables still
    write the legacy name-as-key/inline shape for component services). One normalizer,
    one canonical shape - the editables and the migration no longer each hand-encode it.
    """
    changed = False

    services = project_data.get("services")
    if isinstance(services, list):
        for i, entry in enumerate(services):
            normalized = _normalize_service_entry(entry, "name")
            if normalized is not entry:
                services[i] = normalized
                changed = True

    entities: list[dict[str, Any]] = [c for c in project_data.get("components", []) or [] if isinstance(c, dict)]
    entities.extend(d for d in project_data.get("deployments", []) or [] if isinstance(d, dict))
    for entity in entities:
        entity_services = entity.get("services")
        if not isinstance(entity_services, list):
            continue
        for i, entry in enumerate(entity_services):
            normalized = _normalize_service_entry(entry, "reference")
            if normalized is not entry:
                entity_services[i] = normalized
                changed = True

    return changed


def normalize_domains_location(project_data: dict[str, Any]) -> bool:
    """Relocate the root ``domains:`` approval block under the publish-on-web service
    config: ``services/[publish-on-web]/config/domains`` (v2.4 -> v2.5, RC-5).

    The block is project-global and publish-on-web is a root-level service definition,
    so the service-definition config is its home. Placement is delegated to
    ``ensure_domains_config`` -- the single authority on where the block lives -- so
    this migration and the runtime read/write path (connectors/subdomain.py) never
    disagree. Idempotent: a no-op once the block already lives under the service (or
    there is no block at all).

    Readers accept both locations (``get_domains_config``), so a file that has not been
    migrated yet keeps working and relocates on its next load/save.
    """
    if not isinstance(project_data.get("domains"), dict):
        return False
    from opi.connectors.subdomain import ensure_domains_config

    ensure_domains_config(project_data)
    return True


def relocate_invites_to_service(project_data: dict[str, Any]) -> bool:
    """Relocate the top-level ``invites:`` block to the invite service config (v2.5 -> v2.6, RC-13).

    ``invites.settings.default_language`` + ``invites.active`` move to
    ``services/invite/config`` (find-or-creates the invite service entry, like the keycloak
    ``config.keycloak`` -> ``service realms`` move in ``_migrate_v2_2_to_v2_3``). The block is
    normalized through ``InviteConfig`` and dumped ``by_alias`` so the on-disk result is the
    hyphenated key spelling of the service model and matches the committed schema fragment.

    ``invites`` is a TOP-LEVEL key, so this reads ``project_data`` directly rather than walking
    ``components`` / ``deployments``. Idempotent: once ``invites`` is gone, a no-op. An empty
    ``invites`` block is simply removed (no invite service is created for it).

    Returns True if any change was made.
    """
    invites = project_data.get("invites")
    if not isinstance(invites, dict):
        return False

    from pydantic import ValidationError

    from opi.services.catalog.invite.config_model import InviteConfig
    from opi.services.project import Project

    settings = invites.get("settings") if isinstance(invites.get("settings"), dict) else {}
    active = invites.get("active") or []
    default_language = settings.get("default_language") if isinstance(settings, dict) else None

    if not active and not default_language:
        # Empty legacy block: nothing worth a service entry, just drop it.
        del project_data["invites"]
        return True

    config_input: dict[str, Any] = {"active": active}
    if default_language:
        config_input["default-language"] = default_language

    try:
        model = InviteConfig.model_validate(config_input)
        dumped: dict[str, Any] = model.model_dump(by_alias=True, exclude_unset=True)
    except ValidationError:
        # Defensive: never hard-fail a whole project migration on one odd invite. Relocate the
        # raw block (flattening settings) so the file stays loadable; the config-validation gate
        # surfaces the real problem on the next save.
        logger.warning("Invite block failed model validation during relocation; relocating raw")
        dumped = {"active": active}
        if default_language:
            dumped["default-language"] = default_language

    Project(project_data).set("services/invite/config", dumped)
    del project_data["invites"]
    project_name = project_data.get("name", "unknown")
    logger.info(f"Relocated {len(active)} invite(s) to services/invite/config for project '{project_name}'")
    return True


def relocate_domain_settings_to_service(project_data: dict[str, Any]) -> bool:
    """Move each deployment's web-address settings under publish-on-web (v2.6 -> v2.7, RC-60).

    The seven fields describe one thing -- how publish-on-web composes this deployment's
    hostname and with which certificate -- and they were the last part of that service's
    config still stored outside it. Same shape as ``normalize_domains_location``: the
    placement is delegated to the service's own authority
    (``catalog/publish_on_web/domain_config.relocate_domain_settings``), so the migration and
    the runtime read/write path cannot disagree about where a value lives.

    Idempotent, and deliberately narrow: a deployment with no web-address settings at its
    root is left exactly as it was, so no deployment grows an empty publish-on-web entry it
    never had. Readers accept both locations, so a file that has not been migrated yet keeps
    working and relocates on its next load/save.

    Returns True if any deployment changed.
    """
    from opi.services.catalog.publish_on_web.domain_config import relocate_domain_settings

    changed = False
    for deployment in project_data.get("deployments") or []:
        if isinstance(deployment, dict) and relocate_domain_settings(deployment):
            changed = True
    if changed:
        logger.info(
            f"Relocated deployment web-address settings to the publish-on-web service "
            f"for project '{project_data.get('name', 'unknown')}'"
        )
    return changed


def _migrate_v2_2_to_v2_3(project_data: dict[str, Any]) -> bool:
    """Relocate the per-cluster Keycloak admin connections from the project-level
    ``config.keycloak`` list to the keycloak service's ``config.realms`` (RC-5 B).

    Verbatim move: entries are unchanged (host/realm/username/password/...), still
    matched by ``realm`` downstream as before. The keycloak service entry is
    find-or-created (a project with keycloak connections uses the keycloak service).

    Returns True if any change was made. Idempotent: once ``config.keycloak`` is
    gone, it is a no-op.
    """
    config = project_data.get("config")
    if not isinstance(config, dict) or "keycloak" not in config:
        return False

    kc_list = config.get("keycloak")
    # Lazy import avoids any module-load ordering issues (schema_migration is imported early).
    from opi.services.project import Project

    if kc_list:
        # Move verbatim into services[keycloak].config.realms (find-or-creates the
        # keycloak service entry, preserving any existing keycloak config + order).
        Project(project_data).set("services/keycloak/config/realms", kc_list)
    del config["keycloak"]
    return True


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


#: The migration chain as data: (version this step produces, step). ``migrate_to_latest``
#: runs every step whose version is newer than the file's. Declaring it here rather than
#: as a run of ``if version < X`` lines is what lets the schema check compare the chain
#: against the schemas on disk, so a migration without a schema cannot slip through.
MIGRATION_STEPS: tuple[tuple[int | float, Callable[[dict[str, Any]], bool]], ...] = (
    (2.1, _migrate_v2_to_v2_1),
    (2.2, _migrate_v2_1_to_v2_2),
    (2.3, _migrate_v2_2_to_v2_3),
    (2.4, normalize_service_entries),
    (2.5, normalize_domains_location),
    (2.6, relocate_invites_to_service),
    (2.7, relocate_domain_settings_to_service),
)

# The v1 -> v2 step is the odd one out (it replaces the dict rather than mutating it) and
# runs separately in migrate_to_latest, so the table covers 2.1 and up.
if tuple(version for version, _ in MIGRATION_STEPS) != SCHEMA_VERSIONS[2:]:
    raise RuntimeError(
        f"SCHEMA_VERSIONS {SCHEMA_VERSIONS} and MIGRATION_STEPS "
        f"{tuple(v for v, _ in MIGRATION_STEPS)} disagree: every migration needs a version and vice versa."
    )
