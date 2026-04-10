"""Service-aware path resolution for YAML data.

The project YAML stores services as a mixed list (strings and dicts):

    services:
      - publish-on-web        # string = service without config
      - keycloak:              # dict = service with config
          config:
            template: sso-only

Standard get_value/set_value treat "services" as a dict key, so
paths like "services/keycloak/config/template" fail because
data["services"] is a list, not a dict.

This module provides smart_get_value and smart_set_value that detect
service config paths and resolve them correctly through the list.
"""

from __future__ import annotations

import re
from typing import Any

from opi.forms.editables.path import delete_value, get_value, set_value

_SERVICE_CONFIG_RE = re.compile(r"^services/([^/\[]+)(/(.+))?$")


def is_service_config_path(yaml_path: str) -> bool:
    """Check if a path targets a service's config inside the services list.

    Returns True for paths like:
      - "services/keycloak/config/template"
      - "services/namespace-postgresql-database/config/instances"

    Returns False for:
      - "services" (top-level)
      - "config/age-public-key" (not under services)
      - "users[0]/email"
    """
    return _SERVICE_CONFIG_RE.match(yaml_path) is not None


def parse_service_path(yaml_path: str) -> tuple[str, str | None]:
    """Parse a service config path into (service_name, sub_path).

    Args:
        yaml_path: A path like "services/keycloak/config/template".

    Returns:
        Tuple of (service_name, sub_path_or_None).
        E.g. ("keycloak", "config/template") or ("keycloak", None).

    Raises:
        ValueError: If the path is not a service config path.
    """
    m = _SERVICE_CONFIG_RE.match(yaml_path)
    if not m:
        msg = f"Not a service config path: {yaml_path!r}"
        raise ValueError(msg)
    service_name = m.group(1)
    sub_path = m.group(3)  # group(3) is the part after the second /
    return service_name, sub_path


def find_service_in_list(
    services: list[Any],
    service_name: str,
) -> tuple[int, dict[str, Any] | str | None]:
    """Find a service entry in the mixed services list.

    Args:
        services: The services list (mix of strings and dicts).
        service_name: Service to find (e.g. "keycloak").

    Returns:
        (index, entry) where entry is the raw list item, or (-1, None)
        if not found.
    """
    for i, item in enumerate(services):
        if isinstance(item, str) and item == service_name:
            return i, item
        if isinstance(item, dict) and service_name in item:
            return i, item
    return -1, None


def ensure_service_in_list(
    services: list[Any],
    service_name: str,
) -> tuple[int, dict[str, Any]]:
    """Find or create a service dict entry in the services list.

    If the service exists as a string, promotes it to a dict.
    If it doesn't exist, appends a new dict entry.

    Returns:
        (index, service_dict) - the dict entry for the service.
    """
    idx, entry = find_service_in_list(services, service_name)

    if idx == -1:
        # Service not in list - add it
        new_entry: dict[str, Any] = {service_name: {}}
        services.append(new_entry)
        return len(services) - 1, new_entry

    if isinstance(entry, str):
        # Promote string to dict
        promoted: dict[str, Any] = {service_name: {}}
        services[idx] = promoted
        return idx, promoted

    # Already a dict
    if not isinstance(entry, dict):
        msg = f"Unexpected service entry type: {type(entry)}"
        raise TypeError(msg)
    return idx, entry


def smart_get_value(data: dict[str, Any], yaml_path: str) -> Any:
    """Get a value from YAML data, handling service config paths.

    For service config paths (e.g. "services/keycloak/config/template"),
    navigates through the services list to find the correct entry.

    For all other paths, delegates to the standard get_value.
    """
    if not is_service_config_path(yaml_path):
        return get_value(data, yaml_path)

    services = data.get("services")
    if not isinstance(services, list):
        return None

    service_name, sub_path = parse_service_path(yaml_path)
    idx, entry = find_service_in_list(services, service_name)

    if idx == -1:
        return None

    if isinstance(entry, str):
        # String entry has no config
        return None

    if not isinstance(entry, dict):
        return None

    service_data = entry.get(service_name)
    if sub_path is None:
        return service_data

    if not isinstance(service_data, dict):
        return None

    return get_value(service_data, sub_path)


def smart_path_exists(data: dict[str, Any], yaml_path: str) -> bool:
    """Check whether a path exists in the form data.

    Unlike ``smart_get_value``, this treats a service string entry
    (e.g. ``"keycloak"`` in the services list) as *present* for the
    path ``services/keycloak``.  For deeper paths it checks that the
    nested value is truthy.

    Non-service paths delegate to ``get_value`` and check truthiness.
    """
    if not is_service_config_path(yaml_path):
        return bool(get_value(data, yaml_path))

    services = data.get("services")
    if not isinstance(services, list):
        return False

    service_name, sub_path = parse_service_path(yaml_path)
    idx, entry = find_service_in_list(services, service_name)

    if idx == -1:
        return False

    # "services/keycloak" - service just needs to be in the list
    if sub_path is None:
        return True

    # Deeper path - need a dict entry with config
    if isinstance(entry, str):
        return False
    if not isinstance(entry, dict):
        return False

    service_data = entry.get(service_name)
    if not isinstance(service_data, dict):
        return False

    return bool(get_value(service_data, sub_path))


def check_requirements(
    requires: list[str],
    form_data: dict[str, Any],
) -> list[str]:
    """Return the subset of *requires* paths that are NOT met.

    Each requirement path is checked with ``smart_path_exists``.
    An empty return list means all requirements are satisfied.
    """
    return [path for path in requires if not smart_path_exists(form_data, path)]


def smart_set_value(data: dict[str, Any], yaml_path: str, value: Any) -> dict[str, Any]:
    """Set a value in YAML data, handling service config paths.

    For service config paths:
    - Finds or creates the service entry in the services list
    - Promotes string entries to dicts when needed
    - Sets the config sub-path within the service dict

    For all other paths, delegates to the standard set_value.
    """
    if not is_service_config_path(yaml_path):
        return set_value(data, yaml_path, value)

    service_name, sub_path = parse_service_path(yaml_path)

    # Ensure services list exists
    if "services" not in data or not isinstance(data["services"], list):
        data["services"] = []

    _idx, service_entry = ensure_service_in_list(data["services"], service_name)

    if sub_path is None:
        # Setting the service's entire config
        service_entry[service_name] = value
    else:
        # Ensure the service has a dict to navigate into
        if not isinstance(service_entry[service_name], dict):
            service_entry[service_name] = {}
        set_value(service_entry[service_name], sub_path, value)

    return data


def smart_delete_value(data: dict[str, Any], yaml_path: str) -> None:
    """Remove a key from YAML data, handling service config paths.

    For service config paths with a sub-path (e.g.
    ``services/keycloak/config/restrict-access/realm-role``), deletes
    only the targeted config key - the service entry itself is kept.

    For top-level service paths (no sub-path), removes the service
    entry from the list.

    For all other paths, delegates to the standard delete_value.
    """
    if not is_service_config_path(yaml_path):
        delete_value(data, yaml_path)
        return

    services = data.get("services")
    if not isinstance(services, list):
        return

    service_name, sub_path = parse_service_path(yaml_path)
    idx, entry = find_service_in_list(services, service_name)
    if idx == -1:
        return

    if sub_path is None:
        # No sub-path: remove the entire service entry
        services.pop(idx)
    elif isinstance(entry, dict):
        # Has sub-path: delete only the nested config key
        service_data = entry.get(service_name)
        if isinstance(service_data, dict):
            delete_value(service_data, sub_path)
