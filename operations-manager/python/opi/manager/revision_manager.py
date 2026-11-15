"""Revision history of versioned resources (databases, MinIO buckets), for lifecycle
tracking and cleanup.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opi.services.project import Project
from opi.services.services import service_entry_config, service_entry_name

if TYPE_CHECKING:
    from opi.services.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)


class RevisionManager:
    """Tracks the lifecycle of databases and buckets through the operations that create a
    new generation: clone, restore and the rest.

    Entry lookup goes through ``service_entry_name`` / ``service_entry_config``, so every
    form a services list may hold is found, not just ``reference`` records.
    """

    def __init__(self, project_file_handler: ProjectFileHandler) -> None:
        self._handler = project_file_handler

    def _get_service_config(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> dict[str, Any] | None:
        """The config dict of a deployment service, whatever entry form it uses, or None.

        Identity goes through ``service_entry_name``, never through a key lookup: a services
        list holds bare strings, ``{name}`` records, ``{reference}`` records and legacy
        single-key dicts, and clone state that is not found is silently lost.
        """
        deployment = Project(project_data).find("deployments", name=deployment_name)
        if deployment is None:
            return None
        for entry in deployment.get("services", []) or []:
            if service_entry_name(entry) != service_type:
                continue
            config = service_entry_config(entry)
            return config if isinstance(config, dict) else None
        return None

    def _ensure_service_entry(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> dict[str, Any] | None:
        """Find-or-create the config dict of a deployment service, or None when the
        deployment is not there.

        Reuses whatever entry is already there instead of appending a second one: a services
        list is a selection set, so a duplicate name makes ``validate_project_structure``
        reject the whole project file. A bare string is promoted in place to a record.
        """
        deployment = Project(project_data).find("deployments", name=deployment_name)
        if deployment is None:
            logger.warning(f"Deployment '{deployment_name}' not found in project data")
            return None

        services = deployment.setdefault("services", [])

        for index, entry in enumerate(services):
            if service_entry_name(entry) != service_type:
                continue
            if isinstance(entry, str):
                # Bare selection: promote in place, keeping the position in the list.
                promoted: dict[str, Any] = {"reference": service_type, "config": {}}
                services[index] = promoted
                return promoted["config"]
            if "name" in entry or "reference" in entry:
                if not isinstance(entry.get("config"), dict):
                    entry["config"] = {}
                return entry["config"]
            # Legacy single-key dict: the config lives inside the name-keyed body.
            body = entry.get(service_type)
            if not isinstance(body, dict):
                body = {}
                entry[service_type] = body
            if not isinstance(body.get("config"), dict):
                body["config"] = {}
            return body["config"]

        # Not selected yet: append. ``reference`` is the form every deployment-level entry
        # in production already uses, so nothing churns.
        new_entry: dict[str, Any] = {"reference": service_type, "config": {}}
        services.append(new_entry)
        return new_entry["config"]

    def get_revisions(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> list[dict[str, Any]]:
        """Every revision entry of a deployment service, or [] when there are none."""
        config = self._get_service_config(project_data, deployment_name, service_type)
        if config is None:
            return []

        revisions = config.get("revisions", [])
        return revisions if isinstance(revisions, list) else []

    def get_active_revision(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> dict[str, Any] | None:
        """The currently active revision entry of a service, or None."""
        # Via get_revisions, so entry-form handling lives in one place instead of in a
        # JSONPath that only matched ``reference`` records.
        for revision in self.get_revisions(project_data, deployment_name, service_type):
            if isinstance(revision, dict) and revision.get("status") == "active":
                return revision
        return None

    def record_clone(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int | None,
        resource_name: str,
        source: str,
    ) -> dict[str, Any]:
        """Record a clone as a new active revision, and return the updated project data.

        Args:
            resource_name: The actual resource name, e.g. "project_deployment_v2".
            source: Source reference, e.g. "deployment:production" or
                "external:host:port/db".
        """
        return self._add_revision_entry(
            project_data, deployment_name, service_type, generation, resource_name, "clone", source
        )

    def record_restore(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int | None,
        resource_name: str,
        backup_reference: str,
    ) -> dict[str, Any]:
        """Record a restore as a new active revision, and return the updated project data.

        Args:
            backup_reference: Reference to the backup, e.g. "backup:2026-02-01".
        """
        return self._add_revision_entry(
            project_data, deployment_name, service_type, generation, resource_name, "restore", backup_reference
        )

    def record_initial(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int | None,
        resource_name: str,
    ) -> dict[str, Any]:
        """Record the initial creation, and return the updated project data."""
        return self._add_revision_entry(
            project_data, deployment_name, service_type, generation, resource_name, "initial", None
        )

    def _write_revision_to_config(
        self,
        config: dict[str, Any],
        log_prefix: str,
        generation: int | None,
        resource_name: str,
        action: str,
        source: str | None,
    ) -> None:
        """Write revision data into a config dict, for both the deployment and the component
        level.

        Args:
            config: The config dict to write into; must be mutable.
            log_prefix: Prefix for log messages, e.g. "staging/postgresql-database".
            generation: 0 is the normal name, above 0 a versioned one. None means "no
                generation known" and is written as 0, which is what a caller cloning a
                first-generation resource has.
            action: "clone", "restore", "initial".
        """
        if generation is None:
            generation = 0

        timestamp = datetime.now(UTC).isoformat()

        if "revisions" not in config or not isinstance(config["revisions"], list):
            config["revisions"] = []

        revisions = config["revisions"]

        for entry in revisions:
            if entry.get("status") == "active":
                entry["status"] = "superseded"
                entry["superseded_at"] = timestamp
                logger.debug(f"Marked generation {entry.get('generation')} as superseded for {log_prefix}")

        revisions.insert(
            0,
            {
                "generation": generation,
                "resource": resource_name,
                "status": "active",
                "created_at": timestamp,
                "actions": [{"timestamp": timestamp, "type": action, "source": source}],
            },
        )

        config["generation"] = generation

        logger.info(f"Added revision for {log_prefix}: gen={generation}, resource={resource_name}")

    def _add_revision_entry(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int | None,
        resource_name: str,
        action: str,
        source: str | None,
    ) -> dict[str, Any]:
        """Add a new revision entry at deployment level, marking old active as superseded."""
        config = self._ensure_service_entry(project_data, deployment_name, service_type)
        if config is None:
            return project_data

        self._write_revision_to_config(
            config, f"{deployment_name}/{service_type}", generation, resource_name, action, source
        )
        return project_data

    def _ensure_component_service_config(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_name: str,
        service_type: str,
        reference_name: str,
    ) -> dict[str, Any] | None:
        """The config dict at component level, created when absent, or None when the
        deployment or the component is not there.

        Path: deployments[name].components[reference==component_name]
        .services.{service_type}[reference==reference_name].config
        """
        deployment = Project(project_data).find("deployments", name=deployment_name)
        component = Project.locate((deployment or {}).get("components") or [], reference=component_name)
        if component is None:
            logger.warning(f"Component '{component_name}' in deployment '{deployment_name}' not found in project data")
            return None

        if "services" not in component:
            component["services"] = {}
        if service_type not in component["services"]:
            component["services"][service_type] = []

        service_items = component["services"][service_type]
        if not isinstance(service_items, list):
            logger.warning(
                f"Expected list for {deployment_name}/{component_name}/{service_type}, "
                f"got {type(service_items).__name__}"
            )
            return None

        item = Project.locate(service_items, reference=reference_name)
        if item is not None:
            if "config" not in item:
                item["config"] = {}
            return item["config"]

        new_item: dict[str, Any] = {"reference": reference_name, "config": {}}
        service_items.append(new_item)
        return new_item["config"]

    def record_component_clone(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_name: str,
        service_type: str,
        reference_name: str,
        generation: int,
        resource_name: str,
        source: str,
    ) -> dict[str, Any]:
        """Record a clone at component level, for a PVC or persistent-storage mount.

        Writes to deployments[name].components[ref].services.{service_type}[ref].config,
        where ``record_clone`` writes at deployment level.

        Args:
            reference_name: The service item, e.g. a storage name like "data".
            source: Source reference, e.g. "deployment:production".
        """
        config = self._ensure_component_service_config(
            project_data, deployment_name, component_name, service_type, reference_name
        )
        if config is None:
            return project_data

        log_prefix = f"{deployment_name}/{component_name}/{service_type}/{reference_name}"
        self._write_revision_to_config(config, log_prefix, generation, resource_name, "clone", source)
        return project_data

    def add_action(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int,
        action: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Add another action to an existing revision entry.

        Args:
            action: "backup", "restore".

        Returns the updated project data.
        """
        revision = next(
            (
                r
                for r in self.get_revisions(project_data, deployment_name, service_type)
                if isinstance(r, dict) and r.get("generation") == generation
            ),
            None,
        )

        if revision is None:
            logger.warning(f"Revision not found for {deployment_name}/{service_type} generation {generation}")
            return project_data

        if "actions" not in revision or not isinstance(revision["actions"], list):
            revision["actions"] = []

        revision["actions"].append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": action,
                "source": source,
            }
        )

        logger.debug(f"Added action '{action}' to {deployment_name}/{service_type} generation {generation}")
        return project_data

    def prune(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        max_superseded_entries: int = 5,
    ) -> dict[str, Any]:
        """Keep every active entry and the ``max_superseded_entries`` most recent superseded
        ones, dropping the rest.
        """
        config = self._get_service_config(project_data, deployment_name, service_type)
        if config is None:
            return project_data

        revisions = config.get("revisions", [])
        if not isinstance(revisions, list) or not revisions:
            return project_data

        active = [r for r in revisions if r.get("status") == "active"]
        superseded = [r for r in revisions if r.get("status") == "superseded"]

        superseded.sort(key=lambda x: x.get("superseded_at", ""), reverse=True)
        pruned_count = max(0, len(superseded) - max_superseded_entries)
        if pruned_count > 0:
            superseded = superseded[:max_superseded_entries]
            logger.info(f"Pruned {pruned_count} old revisions from {deployment_name}/{service_type}")

        config["revisions"] = active + superseded
        return project_data

    def get_superseded_resources(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> list[dict[str, Any]]:
        """The superseded revision entries, which may still need cleanup."""
        return [
            r
            for r in self.get_revisions(project_data, deployment_name, service_type)
            if isinstance(r, dict) and r.get("status") == "superseded"
        ]
