from __future__ import annotations

from typing import Any, ClassVar


class AdminRequiredEnforcer:
    """Ensures at least one user has role='admin'."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of user dicts, each with 'email' and 'role' keys.
            context: Not used.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If no user has role='admin'.
        """
        if not value or not isinstance(value, list):
            raise ValueError("Er moet minimaal één gebruiker zijn")
        has_admin = any(isinstance(user, dict) and user.get("role") == "admin" for user in value)
        if not has_admin:
            raise ValueError("Er moet minimaal één administrator zijn")
        return value


class UniqueNamesEnforcer:
    """Ensures all items in a sequence have unique values for a given field."""

    def __init__(self, field_name: str = "name") -> None:
        self.field_name = field_name

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of dicts (e.g., components, deployments).
            context: Not used.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If duplicate names found.
        """
        if not value or not isinstance(value, list):
            return value
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get(self.field_name)
                if name:
                    names.append(str(name))
        duplicates = [name for name in set(names) if names.count(name) > 1]
        if duplicates:
            dup_str = ", ".join(sorted(duplicates))
            raise ValueError(f"Dubbele namen gevonden: {dup_str}")
        return value


def extract_service_names(services: list[Any]) -> list[str]:
    """Extract service names from the mixed services list format.

    Handles all formats in the services list:
      - strings: "keycloak"
      - service-keyed dicts: {"keycloak": {"config": ...}}
      - legacy name dicts: {"name": "keycloak"}
    """
    result: list[str] = []
    for svc in services:
        if isinstance(svc, str):
            result.append(svc)
        elif isinstance(svc, dict):
            if "name" in svc:
                result.append(svc["name"])
            else:
                result.extend(svc.keys())
    return result


class ComponentServicesEnforcer:
    """Section-level enforcer: validates component services and manages storage.

    The form edits storage via a virtual ``storage`` key on each component.
    ``prepare()`` extracts storage configs from v2 service entries into that
    key before rendering.  ``enforce()`` validates services and merges
    ``storage`` items back into the appropriate service entries afterward.
    """

    _STORAGE_TYPE_TO_SERVICE: ClassVar[dict[str, str]] = {
        "persistent": "persistent-storage",
        "ephemeral": "temp-storage",
    }
    _STORAGE_SERVICE_TO_TYPE: ClassVar[dict[str, str]] = {
        "persistent-storage": "persistent",
        "temp-storage": "ephemeral",
    }

    def prepare(self, yaml_data: dict[str, Any]) -> dict[str, Any]:
        """Extract storage from component services into a ``storage`` key.

        Called before form rendering so the storage editables (which use
        ``components[*]/storage[*]/...`` paths) can read/display the data.
        """
        import copy

        result = copy.deepcopy(yaml_data)
        for comp in result.get("components", []):
            if not isinstance(comp, dict):
                continue
            storage_items: list[dict[str, Any]] = []
            for entry in comp.get("services", []):
                if not isinstance(entry, dict):
                    continue
                for svc_name, svc_data in entry.items():
                    if svc_name not in self._STORAGE_SERVICE_TO_TYPE:
                        continue
                    if not isinstance(svc_data, dict):
                        continue
                    for item in svc_data.get("config", []):
                        cfg = dict(item)
                        cfg["type"] = self._STORAGE_SERVICE_TO_TYPE[svc_name]
                        storage_items.append(cfg)
            if storage_items:
                comp["storage"] = storage_items
        return result

    def enforce(self, yaml_data: Any, context: dict[str, Any]) -> Any:
        services = extract_service_names(yaml_data.get("services", []))
        if not services:
            return yaml_data

        components = yaml_data.get("components", [])
        for comp in components:
            if not isinstance(comp, dict):
                continue
            comp_services = comp.get("services", [])
            if comp_services == "__all__" or not isinstance(comp_services, list):
                continue
            uses = extract_service_names(comp_services)
            invalid = [s for s in uses if s not in services]
            if invalid:
                comp_name = comp.get("name", "onbekend")
                invalid_str = ", ".join(invalid)
                raise ValueError(
                    f"Component '{comp_name}' gebruikt ongeldige services: {invalid_str}. "
                    f"Beschikbare services: {', '.join(services)}"
                )

            # Merge storage items back into service entries
            storage = comp.pop("storage", None)
            if storage and isinstance(storage, list):
                self._merge_storage_into_services(comp, storage)

        return yaml_data

    def _merge_storage_into_services(self, comp: dict[str, Any], storage_items: list[dict[str, Any]]) -> None:
        """Merge storage items from the virtual ``storage`` key into service entries."""
        from collections import defaultdict

        by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in storage_items:
            if not isinstance(item, dict):
                continue
            storage_type = item.pop("type", "persistent")
            svc_name = self._STORAGE_TYPE_TO_SERVICE.get(storage_type, "persistent-storage")
            by_service[svc_name].append(item)

        services = comp.get("services", [])
        for svc_name, configs in by_service.items():
            # Find or create the service dict entry
            found = False
            for i, entry in enumerate(services):
                if isinstance(entry, str) and entry == svc_name:
                    # Promote string to dict with config
                    services[i] = {svc_name: {"config": configs}}
                    found = True
                    break
                if isinstance(entry, dict) and svc_name in entry:
                    if not isinstance(entry[svc_name], dict):
                        entry[svc_name] = {}
                    entry[svc_name]["config"] = configs
                    found = True
                    break
            if not found:
                services.append({svc_name: {"config": configs}})


class ServiceDependencyEnforcer:
    """Ensures component services are valid project-level services."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of service name strings (component's services).
            context: Must contain 'project_services' key with list of valid service names.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If any service is not in the project services list.
        """
        if not value or not isinstance(value, list):
            return value
        project_services = context.get("project_services", [])
        if not project_services:
            return value
        invalid = [s for s in value if s not in project_services]
        if invalid:
            invalid_str = ", ".join(invalid)
            raise ValueError(f"Ongeldige services: {invalid_str}. Beschikbare services: {', '.join(project_services)}")
        return value
