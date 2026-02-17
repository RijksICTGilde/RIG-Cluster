"""Revision Manager for tracking versioned resource lifecycle.

This module provides centralized management of revision history for versioned
resources (databases, MinIO buckets) to support lifecycle tracking and cleanup.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jsonpath_ng.ext import parse as jsonpath_parse

if TYPE_CHECKING:
    from opi.handlers.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)


class RevisionManager:
    """Manager for tracking revisions of versioned resources.

    Tracks the lifecycle of databases and buckets as they go through
    clone, restore, and other operations that create new generations.

    Uses JSONPath expressions for efficient querying of project data.
    """

    def __init__(self, project_file_handler: "ProjectFileHandler") -> None:
        """Initialize the RevisionManager.

        Args:
            project_file_handler: Handler for project file operations
        """
        self._handler = project_file_handler

    def _jsonpath_find(self, data: dict[str, Any], path: str) -> list[Any]:
        """Find all matches for a JSONPath expression.

        Args:
            data: The dictionary to search
            path: JSONPath expression

        Returns:
            List of matching values (empty if no matches)
        """
        matches = jsonpath_parse(path).find(data)
        return [m.value for m in matches]

    def _jsonpath_find_one(self, data: dict[str, Any], path: str) -> Any | None:
        """Find first match for a JSONPath expression.

        Args:
            data: The dictionary to search
            path: JSONPath expression

        Returns:
            First matching value or None if no matches
        """
        matches = jsonpath_parse(path).find(data)
        return matches[0].value if matches else None

    def _get_service_config(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> dict[str, Any] | None:
        """Get the config dict for a deployment service using JSONPath.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)

        Returns:
            Config dict or None if not found
        """
        path = f"$.deployments[?(@.name=='{deployment_name}')].services[?(@.reference=='{service_type}')].config"
        return self._jsonpath_find_one(project_data, path)

    def _ensure_service_entry(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> dict[str, Any] | None:
        """Ensure a service entry exists for a deployment and service type.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)

        Returns:
            Config dict, or None if deployment not found
        """
        # Find deployment
        deployment_path = f"$.deployments[?(@.name=='{deployment_name}')]"
        deployments = self._jsonpath_find(project_data, deployment_path)

        if not deployments:
            logger.warning(f"Deployment '{deployment_name}' not found in project data")
            return None

        deployment = deployments[0]

        # Ensure services list exists
        if "services" not in deployment:
            deployment["services"] = []

        # Find or create service entry
        service_path = f"$.deployments[?(@.name=='{deployment_name}')].services[?(@.reference=='{service_type}')]"
        services = self._jsonpath_find(project_data, service_path)

        if services:
            service = services[0]
            if "config" not in service or not isinstance(service["config"], dict):
                service["config"] = {}
            return service["config"]

        # Create new entry
        new_entry: dict[str, Any] = {"reference": service_type, "config": {}}
        deployment["services"].append(new_entry)
        return new_entry["config"]

    def get_revisions(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> list[dict[str, Any]]:
        """Get all revision entries for a deployment service.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)

        Returns:
            List of revision entries, empty list if none
        """
        config = self._get_service_config(project_data, deployment_name, service_type)
        if config is None:
            return []

        revisions = config.get("revisions", [])
        return revisions if isinstance(revisions, list) else []

    def get_active_revision(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> dict[str, Any] | None:
        """Get the currently active revision entry for a service.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)

        Returns:
            Active revision entry or None if not found
        """
        # Use JSONPath to find active revision directly
        path = (
            f"$.deployments[?(@.name=='{deployment_name}')]"
            f".services[?(@.reference=='{service_type}')]"
            f".config.revisions[?(@.status=='active')]"
        )
        return self._jsonpath_find_one(project_data, path)

    def record_clone(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int,
        resource_name: str,
        source: str,
    ) -> dict[str, Any]:
        """Record a clone operation, creating a new active revision.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)
            generation: Generation number of the new resource
            resource_name: Actual resource name (e.g., "project_deployment_v2")
            source: Source reference (e.g., "deployment:production", "external:host:port/db")

        Returns:
            Updated project_data dictionary
        """
        return self._add_revision_entry(
            project_data, deployment_name, service_type, generation, resource_name, "clone", source
        )

    def record_restore(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int,
        resource_name: str,
        backup_reference: str,
    ) -> dict[str, Any]:
        """Record a restore operation, creating a new active revision.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)
            generation: Generation number of the new resource
            resource_name: Actual resource name
            backup_reference: Reference to the backup (e.g., "backup:2026-02-01")

        Returns:
            Updated project_data dictionary
        """
        return self._add_revision_entry(
            project_data, deployment_name, service_type, generation, resource_name, "restore", backup_reference
        )

    def record_initial(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int,
        resource_name: str,
    ) -> dict[str, Any]:
        """Record initial resource creation.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)
            generation: Generation number of the resource
            resource_name: Actual resource name

        Returns:
            Updated project_data dictionary
        """
        return self._add_revision_entry(
            project_data, deployment_name, service_type, generation, resource_name, "initial", None
        )

    def _write_revision_to_config(
        self,
        config: dict[str, Any],
        log_prefix: str,
        generation: int,
        resource_name: str,
        action: str,
        source: str | None,
    ) -> None:
        """Write revision data to a config dict.

        This is the shared logic for both deployment-level and component-level revision tracking.

        Args:
            config: The config dict to write to (must be mutable)
            log_prefix: Prefix for log messages (e.g., "staging/postgresql-database")
            generation: Generation number of the resource (0 = normal name, >0 = versioned)
            resource_name: Actual resource name
            action: Action type (e.g., "clone", "restore", "initial")
            source: Source reference or None
        """
        # Never write None as generation - 0 is the default (normal name, no suffix)
        if generation is None:
            generation = 0

        timestamp = datetime.now(UTC).isoformat()

        # Ensure revisions list exists
        if "revisions" not in config or not isinstance(config["revisions"], list):
            config["revisions"] = []

        revisions = config["revisions"]

        # Mark any existing active entries as superseded
        for entry in revisions:
            if entry.get("status") == "active":
                entry["status"] = "superseded"
                entry["superseded_at"] = timestamp
                logger.debug(f"Marked generation {entry.get('generation')} as superseded for {log_prefix}")

        # Create and insert new active entry at beginning
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

        # Update generation in config
        config["generation"] = generation

        logger.info(f"Added revision for {log_prefix}: gen={generation}, resource={resource_name}")

    def _add_revision_entry(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_type: str,
        generation: int,
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
        """Ensure a config dict exists at component level and return it.

        Path: deployments[name].components[reference==component_name]
              .services.{service_type}[reference==reference_name].config

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            service_type: Service type (e.g., "persistent-storage")
            reference_name: Reference name of the service item (e.g., storage name)

        Returns:
            Config dict, or None if deployment/component not found
        """
        for deployment in project_data.get("deployments", []):
            if deployment.get("name") != deployment_name:
                continue

            for component in deployment.get("components", []):
                if component.get("reference") != component_name:
                    continue

                # Ensure services dict exists
                if "services" not in component:
                    component["services"] = {}

                # Ensure service_type list exists
                if service_type not in component["services"]:
                    component["services"][service_type] = []

                service_items = component["services"][service_type]

                if not isinstance(service_items, list):
                    logger.warning(
                        f"Expected list for {deployment_name}/{component_name}/{service_type}, "
                        f"got {type(service_items).__name__}"
                    )
                    return None

                # Find existing item
                for item in service_items:
                    if isinstance(item, dict) and item.get("reference") == reference_name:
                        if "config" not in item:
                            item["config"] = {}
                        return item["config"]

                # Create new item
                new_item: dict[str, Any] = {"reference": reference_name, "config": {}}
                service_items.append(new_item)
                return new_item["config"]

        logger.warning(f"Component '{component_name}' in deployment '{deployment_name}' not found in project data")
        return None

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
        """Record a clone operation at component level (for PVC/persistent-storage).

        Unlike record_clone (deployment level), this writes to:
        deployments[name].components[ref].services.{service_type}[ref].config

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            service_type: Service type (e.g., "persistent-storage")
            reference_name: Reference name (e.g., storage name like "data")
            generation: Generation number of the new resource
            resource_name: Actual resource name (e.g., PVC name)
            source: Source reference (e.g., "deployment:production")

        Returns:
            Updated project_data dictionary
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
        """Add an additional action to an existing revision entry.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)
            generation: Generation number to add action to
            action: Action type (e.g., "backup", "restore")
            source: Optional source/target reference

        Returns:
            Updated project_data dictionary
        """
        # Find revision by generation using JSONPath
        path = (
            f"$.deployments[?(@.name=='{deployment_name}')]"
            f".services[?(@.reference=='{service_type}')]"
            f".config.revisions[?(@.generation=={generation})]"
        )
        revision = self._jsonpath_find_one(project_data, path)

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
        """Prune old superseded entries from revision list.

        Keeps all active entries and up to max_superseded_entries most recent superseded.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)
            max_superseded_entries: Maximum superseded entries to keep (default 5)

        Returns:
            Updated project_data dictionary
        """
        config = self._get_service_config(project_data, deployment_name, service_type)
        if config is None:
            return project_data

        revisions = config.get("revisions", [])
        if not isinstance(revisions, list) or not revisions:
            return project_data

        # Separate by status
        active = [r for r in revisions if r.get("status") == "active"]
        superseded = [r for r in revisions if r.get("status") == "superseded"]

        # Sort superseded by timestamp (most recent first) and limit
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
        """Get list of superseded resources that may need cleanup.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)

        Returns:
            List of superseded revision entries
        """
        # Use JSONPath to find superseded revisions directly
        path = (
            f"$.deployments[?(@.name=='{deployment_name}')]"
            f".services[?(@.reference=='{service_type}')]"
            f".config.revisions[?(@.status=='superseded')]"
        )
        return self._jsonpath_find(project_data, path)
