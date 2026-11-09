"""Adding and removing the extra database schemas of a project (RC-59)."""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from opi.services import ServiceAdapter, ServiceType, ServiceValidationError
from opi.services.catalog.base import ConfigLayer
from opi.services.postgres_scope import schema_is_marked
from opi.services.project import Project
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError

if TYPE_CHECKING:
    from opi.manager.project.access import ProjectFileAccess

logger = logging.getLogger(__name__)


class DatabaseSchemaMutations:
    """The database-schema writer of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ProjectFileAccess) -> None:
        self._host = host

    async def manage_database_schemas(
        self,
        operation: str,
        postfix: str,
        *,
        description: str = "",
        forget: bool = False,
    ) -> dict[str, Any]:
        """Add or remove one extra database schema without resending the rest of the config (RC-59).

        * ``add`` puts a new schema in the list. A postfix that is already there and active
          is a conflict; one that is there but marked comes back, with the data that
          marking rather than dropping kept (RC-17).
        * ``remove`` marks. The schema and its data stay in PostgreSQL, and the entry stays
          in the project file recording that it exists.
        * ``remove`` with ``forget`` takes the entry out of the file. The data still stays
          in PostgreSQL, nothing here ever drops a schema, but the project no longer
          records that the schema exists, so it cannot be brought back through the API.

        Uniqueness, the 63-character limit on the full name and collisions with the
        variables the database service already exposes are NOT re-checked here: they are
        enforced at save time, by the same chokepoint the wizard hits, and surface as
        ``validation_error``.
        """
        service_name = ServiceType.POSTGRESQL_DATABASE.value
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            view = Project(project_data)
            if not view.uses_service(service_name):
                return {
                    "success": False,
                    "error": f"Project '{project_name}' gebruikt de dienst '{service_name}' niet",
                    "error_type": "not_found",
                }

            raw_config = view.service_config(service_name)
            config: dict[str, Any] = copy.deepcopy(raw_config) if isinstance(raw_config, dict) else {}
            entries: list[dict[str, Any]] = list(config.get("schemas") or [])
            index = next((i for i, entry in enumerate(entries) if entry.get("postfix") == postfix), None)

            if operation == "add":
                if index is None:
                    entries.append({"postfix": postfix, "description": description})
                    outcome = {"created": True, "restored": False}
                elif not schema_is_marked(entries[index]):
                    return {
                        "success": False,
                        "error": f"Schema '{postfix}' bestaat al in dit project",
                        "error_type": "conflict",
                    }
                else:
                    entry = dict(entries[index])
                    entry.pop("marked-for-deletion", None)
                    entry.pop("marked_for_deletion", None)
                    if description:
                        entry["description"] = description
                    entries[index] = entry
                    outcome = {"created": False, "restored": True}
            elif operation == "remove":
                if index is None:
                    return {
                        "success": False,
                        "error": f"Schema '{postfix}' bestaat niet in dit project",
                        "error_type": "not_found",
                    }
                if forget:
                    entries.pop(index)
                    outcome = {"marked": False, "forgotten": True}
                elif schema_is_marked(entries[index]):
                    # Already on its way out: nothing to write.
                    return {"success": True, "changed": False, "marked": True, "forgotten": False}
                else:
                    entries[index] = {**entries[index], "marked-for-deletion": True}
                    outcome = {"marked": True, "forgotten": False}
            else:
                return {"success": False, "error": f"Unknown operation '{operation}'", "error_type": "invalid_request"}

            config["schemas"] = entries
            try:
                ServiceAdapter.set_service_config(project_data, service_name, ConfigLayer.PROJECT, config)
            except ServiceValidationError as e:
                return {"success": False, "error": str(e), "error_type": "validation_error"}

            commit_message = f"{operation.capitalize()} database schema '{postfix}' in project '{project_name}'"
            try:
                await self._host.save_and_commit_project(project_data, commit_message)
            except (ProjectSchemaError, ProjectIntegrityError) as e:
                return {"success": False, "error": str(e), "error_type": "validation_error"}

            logger.info("Schema '%s' %s in project '%s'", postfix, operation, project_name)
            return {"success": True, "changed": True, **outcome}

        except Exception as e:
            error_msg = f"Error managing database schema '{postfix}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}
