"""
Project File Handler for managing project YAML files and change detection.

This module provides functionality to read, parse, and analyze changes in project files
including git-based diff generation and structured change extraction.
"""

import logging
import re
from typing import Any

from deepdiff import DeepDiff
from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml import YAML

from opi.connectors.git import GitConnector
from opi.services import ServiceAdapter, ServiceType
from opi.services.resource_analyzer import _k8s_memory_to_mb
from opi.utils.age import decrypt_password_smart_sync, get_decoded_project_private_key
from opi.utils.env_vars import validate_and_parse_env_vars
from opi.utils.yaml_util import save_yaml_to_path

logger = logging.getLogger(__name__)

# Default resource values for deployment containers
DEFAULT_RESOURCES: dict[str, str] = {
    "requests_memory": "128Mi",
    "requests_cpu": "50m",
    "limits_memory": "512Mi",
    "limits_cpu": "500m",
}


def _parse_resources_block(raw: dict | None, defaults: dict[str, str] | None = None) -> dict[str, str]:
    """
    Parse a nested resources block into flat keys with defaults.

    Args:
        raw: Nested dict like {"requests": {"memory": "256Mi"}, "limits": {"cpu": "500m"}}
        defaults: Default values to use for missing keys (uses DEFAULT_RESOURCES if None)

    Returns:
        Flat dict with all 4 keys filled in
    """
    if defaults is None:
        defaults = DEFAULT_RESOURCES

    if not raw or not isinstance(raw, dict):
        return defaults.copy()

    requests = raw.get("requests", {}) or {}
    limits = raw.get("limits", {}) or {}

    return {
        "requests_memory": str(requests.get("memory", defaults["requests_memory"])),
        "requests_cpu": str(requests.get("cpu", defaults["requests_cpu"])),
        "limits_memory": str(limits.get("memory", defaults["limits_memory"])),
        "limits_cpu": str(limits.get("cpu", defaults["limits_cpu"])),
    }


def _parse_resources_block_partial(raw: dict | None) -> dict[str, str]:
    """
    Parse a nested resources block into flat keys without filling defaults.

    Only returns keys that are explicitly set in the raw block.

    Args:
        raw: Nested dict like {"limits": {"memory": "2048Mi"}}

    Returns:
        Flat dict with only the keys that were explicitly set
    """
    if not raw or not isinstance(raw, dict):
        return {}

    result: dict[str, str] = {}
    requests = raw.get("requests", {}) or {}
    limits = raw.get("limits", {}) or {}

    if "memory" in requests:
        result["requests_memory"] = str(requests["memory"])
    if "cpu" in requests:
        result["requests_cpu"] = str(requests["cpu"])
    if "memory" in limits:
        result["limits_memory"] = str(limits["memory"])
    if "cpu" in limits:
        result["limits_cpu"] = str(limits["cpu"])

    return result


def _apply_flat_resources(target: dict[str, Any], resources: dict[str, str]) -> None:
    """Write flat resource keys into a nested resources block.

    Ensures the ``requests``/``limits`` structure exists, applies values,
    and removes the legacy ``memory`` sub-dict (which used ``request``/``limit``
    keys) to prevent dual-format entries.
    """
    if "resources" not in target:
        target["resources"] = {}
    res = target["resources"]
    if "requests" not in res:
        res["requests"] = {}
    if "limits" not in res:
        res["limits"] = {}
    if "requests_memory" in resources:
        res["requests"]["memory"] = resources["requests_memory"]
    if "requests_cpu" in resources:
        res["requests"]["cpu"] = resources["requests_cpu"]
    if "limits_memory" in resources:
        res["limits"]["memory"] = resources["limits_memory"]
    if "limits_cpu" in resources:
        res["limits"]["cpu"] = resources["limits_cpu"]
    # Remove legacy format (memory/request, memory/limit)
    res.pop("memory", None)


class ProjectFileHandler:
    """Handler for project file operations including reading, parsing, and change detection."""

    def __init__(self) -> None:
        """Initialize the ProjectFileHandler."""
        logger.debug("Initializing ProjectFileHandler")
        self._project_data: dict[str, str | list | dict[str, str]] | None = None
        self._full_file_path: str | None = None

    def _normalize_age_content(self, age_content: str) -> str:
        """
        Normalize AGE encrypted content by converting escaped newlines to actual newlines.

        This handles cases where AGE content is stored as a quoted string with \n escapes
        instead of a literal YAML block.

        Args:
            age_content: AGE encrypted content (may have escaped newlines)

        Returns:
            Normalized AGE content with proper newlines
        """
        if not age_content or "-----BEGIN AGE ENCRYPTED FILE-----" not in age_content:
            return age_content

        # Replace escaped newlines with actual newlines
        normalized = age_content.replace("\\n", "\n")

        logger.debug("Normalized AGE content format (converted escaped newlines)")
        return normalized

    def _looks_like_env_format(self, text: str) -> bool:
        """
        Check if text looks like ENV format (KEY=VALUE lines).
        Checks only the first 1-2 non-empty, non-comment lines.

        Args:
            text: Text to check

        Returns:
            True if text appears to be in ENV format
        """
        if not text or not isinstance(text, str):
            return False

        lines = text.strip().split("\n")
        checked_lines = 0

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # ENV format: KEY=VALUE where KEY starts with letter/underscore
            # and contains only letters, numbers, and underscores
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
                return True

            checked_lines += 1
            # Only check first 2 actual content lines
            if checked_lines >= 2:
                break

        return False

    def _decrypt_with_private_key(self, encrypted_value: str, private_key: str | None) -> Any:
        """
        Decrypt an AGE encrypted value using the provided private key and parse as YAML if applicable.

        Args:
            encrypted_value: The AGE encrypted content (may have escaped newlines)
            private_key: The private key (may itself be encrypted and have escaped newlines)

        Returns:
            Decrypted and parsed value (string, dict, list, etc.) or original value if decryption fails
        """
        if not private_key:
            raise ValueError("Private key is required for decryption")

        # Normalize both the encrypted value and private key to handle escaped newlines
        normalized_encrypted_value = self._normalize_age_content(encrypted_value)
        normalized_private_key = self._normalize_age_content(private_key)

        decrypted_value = decrypt_password_smart_sync(normalized_encrypted_value, normalized_private_key)

        # Check if decrypted content looks like ENV format (KEY=VALUE lines)
        # If so, return as-is and let validate_and_parse_env_vars handle it
        if self._looks_like_env_format(decrypted_value):
            logger.debug("Decrypted content appears to be ENV format, returning as string")
            return decrypted_value

        # Try to parse as YAML in case the decrypted content is a YAML block
        try:
            yaml = YAML()
            parsed_value = yaml.load(decrypted_value)
            logger.debug(f"Successfully parsed decrypted content as YAML: {type(parsed_value)}")
            return parsed_value
        except Exception:
            # If YAML parsing fails, return the decrypted string as-is
            logger.debug("Decrypted content is not valid YAML, returning as string")
            return decrypted_value

    async def read_project_file(self, full_file_path: str) -> dict[str, str | list | dict[str, str]]:
        """
        Read and parse a project YAML file. Keeps it in memory for future use.

        Args:
            full_file_path: Path to the project YAML file

        Returns:
            Dictionary containing the parsed project data

        Raises:
            Exception: If file cannot be read or parsed
        """
        if self._full_file_path and self._full_file_path != full_file_path:
            raise Exception("Can only initialize one project file per class")

        if not self._project_data:
            self._project_data = await self._parse_project_file(full_file_path)
            self._full_file_path = full_file_path
        return self._project_data

    async def _parse_project_file(self, file_path: str) -> dict[str, str | list | dict[str, str]]:
        """
        Parse a project YAML file.

        Args:
            file_path: Path to the project YAML file

        Returns:
            Dictionary containing the parsed project data
        """
        logger.debug(f"Parsing project file: {file_path}")
        # TODO add schema and do schema validation
        # TODO check if yaml references are valid
        try:
            yaml = YAML()
            with open(file_path) as f:
                project_data = yaml.load(f)
            logger.debug(f"Successfully parsed project file: {file_path}")
            return project_data
        except Exception as e:
            # TODO: do not log and re-raise
            logger.exception(f"Error parsing project file: {e}")
            raise

    # TODO: should this method be here or moved?
    async def get_previous_yaml_content(self, git_connector: GitConnector, file_path: str) -> dict[str, Any] | None:
        """
        Get the previous version of a YAML file from git history.

        Args:
            git_connector: GitConnector to access the repository
            file_path: Path to the YAML file within the repository

        Returns:
            Parsed YAML content from the previous commit, or None if no previous version exists
        """
        try:
            # Use GitConnector to get the previous file content
            previous_content_str = await git_connector.get_previous_file_content(file_path)

            if previous_content_str is None:
                logger.debug(f"No previous version found for {file_path}")
                return None

            # Parse the YAML content
            yaml = YAML()
            previous_content = yaml.load(previous_content_str)
            logger.debug(f"Successfully retrieved and parsed previous version of {file_path}")
            return previous_content

        except Exception as e:
            logger.warning(f"Error retrieving previous YAML content: {e}")
            return None

    def generate_yaml_diff(self, current_yaml: dict[str, Any], previous_yaml: dict[str, Any] | None) -> dict[str, Any]:
        """
        Generate a diff between current and previous YAML content using DeepDiff.

        Args:
            current_yaml: Current YAML content as dictionary
            previous_yaml: Previous YAML content as dictionary, or None for new file

        Returns:
            Dictionary containing the DeepDiff result
        """
        if previous_yaml is None:
            logger.info("No previous version found - treating all content as new")
            return {"dictionary_item_added": {"root": current_yaml}}

        diff = DeepDiff(previous_yaml, current_yaml, ignore_order=True)
        logger.debug(f"Generated diff with keys: {list(diff.keys())}")

        return diff

    def extract_changes_from_diff(self, diff: dict[str, Any], current_yaml: dict[str, Any]) -> dict[str, Any]:
        """
        Extract added, changed, and deleted items from a DeepDiff result.

        Args:
            diff: DeepDiff result dictionary
            current_yaml: Current YAML content for reference

        Returns:
            Dictionary with keys 'added', 'changed', 'deleted' containing the respective changes
        """
        changes = {"added": {}, "changed": {}, "deleted": {}}

        # Handle added items (dictionary_item_added, iterable_item_added)
        # dictionary_item_added is a SetOrdered of path strings (no values);
        # iterable_item_added is a dict of path -> value.
        if "dictionary_item_added" in diff:
            for path in diff["dictionary_item_added"]:
                clean_path = self._parse_deepdiff_path(path)
                changes["added"][clean_path] = self._resolve_deepdiff_value(current_yaml, path)
                logger.debug(f"Added: {clean_path}")

        if "iterable_item_added" in diff:
            for path, value in diff["iterable_item_added"].items():
                clean_path = self._parse_deepdiff_path(path)
                changes["added"][clean_path] = value
                logger.debug(f"Added (iterable): {clean_path}")

        # Handle removed items (dictionary_item_removed, iterable_item_removed)
        # dictionary_item_removed is a SetOrdered (no values).
        if "dictionary_item_removed" in diff:
            for path in diff["dictionary_item_removed"]:
                clean_path = self._parse_deepdiff_path(path)
                changes["deleted"][clean_path] = None
                logger.debug(f"Deleted: {clean_path}")

        if "iterable_item_removed" in diff:
            for path, value in diff["iterable_item_removed"].items():
                clean_path = self._parse_deepdiff_path(path)
                changes["deleted"][clean_path] = value
                logger.debug(f"Deleted (iterable): {clean_path}")

        # Handle value changes (values_changed)
        if "values_changed" in diff:
            for path, change in diff["values_changed"].items():
                clean_path = self._parse_deepdiff_path(path)
                changes["changed"][clean_path] = {"old_value": change["old_value"], "new_value": change["new_value"]}
                logger.debug(f"Changed: {clean_path}")

        logger.info(
            f"Extracted changes - Added: {len(changes['added'])}, Changed: {len(changes['changed'])}, Deleted: {len(changes['deleted'])}"
        )
        return changes

    def _parse_deepdiff_path(self, path: str) -> str:
        """
        Parse a DeepDiff path into a more readable format.

        Args:
            path: DeepDiff path like "root['deployments']['web-app']"

        Returns:
            Cleaned path like "deployments.web-app"
        """
        # Remove 'root' and convert ['key'] to .key
        clean_path = path.removeprefix("root")
        # Convert ['key'] to .key, handling nested brackets
        clean_path = re.sub(r"\['([^']+)']", r".\1", clean_path)
        # Convert bare numeric indices [0] to .0
        clean_path = re.sub(r"\[(\d+)]", r".\1", clean_path)
        # Remove leading dot
        clean_path = clean_path.lstrip(".")

        return clean_path

    @staticmethod
    def _resolve_deepdiff_value(data: dict[str, Any], deepdiff_path: str) -> Any:
        """Resolve a DeepDiff path like ``root['key1']['key2']`` to the value in *data*."""
        # Strip "root" prefix and extract bracket segments
        segments = re.findall(r"\['([^']+)'\]|\[(\d+)\]", deepdiff_path)
        current: Any = data
        for str_key, int_key in segments:
            if str_key:
                if isinstance(current, dict):
                    current = current.get(str_key)
                else:
                    return None
            elif int_key:
                idx = int(int_key)
                if isinstance(current, list) and idx < len(current):
                    current = current[idx]
                else:
                    return None
        return current

    async def analyze_project_changes(
        self, git_connector: GitConnector, full_file_path: str, relative_file_path: str
    ) -> dict[str, Any]:
        """
        Analyze changes between current and previous versions of a project file.

        Args:
            git_connector: GitConnector to access the repository
            relative_file_path: Relative path to the YAML file within the repository
            full_file_path: Full path to the YAML file on the OS

        Returns:
            Dictionary containing:
            - current_yaml: Current YAML content
            - previous_yaml: Previous YAML content (or None)
            - diff: DeepDiff result
            - changes: Structured changes with added/changed/deleted items
        """
        # Read current YAML content
        current_yaml = await self.read_project_file(full_file_path)

        # Get previous YAML content from git history
        previous_yaml = await self.get_previous_yaml_content(git_connector, relative_file_path)

        # Generate diff using DeepDiff
        diff = self.generate_yaml_diff(current_yaml, previous_yaml)

        # Extract structured changes
        changes = self.extract_changes_from_diff(diff, current_yaml)

        return {"current_yaml": current_yaml, "previous_yaml": previous_yaml, "diff": diff, "changes": changes}

    def extract_value_by_path(
        self, data: dict[str, Any], path: str, default: Any = None, private_key: str | None = None
    ) -> Any:
        """
        Extract a value from nested dictionary using JSONPath expression.

        Args:
            data: The dictionary to extract from
            path: JSONPath expression (e.g., "$.components[?(@.name=='frontend')].ports.inbound[0]")
            default: Default value to return if path is not found
            private_key: AGE private key for decrypting encrypted values

        Returns:
            - For array wildcard queries (e.g., [*]): Always returns list of all matched values
            - For single match queries (e.g., [0] or specific filters): The extracted value
            - If no matches: default value
        """
        jsonpath_expr = jsonpath_parse(path)
        matches = jsonpath_expr.find(data)

        if not matches:
            return default

        # If path contains [*], always return a list (even for single match)
        # This ensures consistent return types for array queries
        if "[*]" in path:
            if private_key:
                return [
                    self._decrypt_with_private_key(match.value, private_key)
                    if isinstance(match.value, str)
                    else match.value
                    for match in matches
                ]
            else:
                return [match.value for match in matches]

        # If multiple matches (but not explicitly using [*]), return all values as a list
        if len(matches) > 1:
            if private_key:
                return [
                    self._decrypt_with_private_key(match.value, private_key)
                    if isinstance(match.value, str)
                    else match.value
                    for match in matches
                ]
            else:
                return [match.value for match in matches]

        # Single match - return the value directly
        # Only decrypt if value is a string (not already a dict/list)
        value = matches[0].value
        if private_key and isinstance(value, str):
            return self._decrypt_with_private_key(value, private_key)
        else:
            return value

    def extract_component_port(self, project_data: dict[str, Any], component_name: str, default_port: int = 80) -> int:
        """
        Extract the first inbound port from a component definition by name.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find
            default_port: Default port to return if not found

        Returns:
            The first inbound port of the component or default_port if not found
        """
        # Use JSONPath with extended parser to find the component by name and extract its first inbound port
        path = f"$.components[?(@.name='{component_name}')].ports.inbound[0]"
        port = self.extract_value_by_path(project_data, path, default_port)

        if port != default_port:
            logger.info(f"Found port {port} for component '{component_name}'")
        else:
            logger.warning(f"No inbound port found for component '{component_name}', using default {default_port}")

        return port

    def component_has_ports(self, project_data: dict[str, Any], component_name: str) -> bool:
        """
        Check if a component has at least one inbound port configured.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to check

        Returns:
            True if the component has at least one inbound port, False otherwise
        """
        path = f"$.components[?(@.name='{component_name}')].ports.inbound[0]"
        # Use None as default to distinguish "not found" from "found with value"
        port = self.extract_value_by_path(project_data, path, None)
        has_ports = port is not None
        logger.debug(f"Component '{component_name}' has ports configured: {has_ports}")
        return has_ports

    def extract_component_path(self, project_data: dict[str, Any], component_name: str, default_path: str = "/") -> str:
        """
        Extract the publication path from a component definition by name.

        This method returns the first path as a string for backward compatibility.
        For multiple paths, use extract_component_paths() instead.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find
            default_path: Default path to return if not found (default: "/")

        Returns:
            The publication path of the component or default_path if not found
        """
        # Use JSONPath with extended parser to find the component by name and extract its path
        json_path = f"$.components[?(@.name='{component_name}')].path"
        path_config = self.extract_value_by_path(project_data, json_path, default_path)

        # Handle both string and list formats
        if isinstance(path_config, str):
            component_path = path_config
        elif isinstance(path_config, list) and path_config:
            # Return first path for backward compatibility
            first_item = path_config[0]
            if isinstance(first_item, dict):  # noqa: SIM108
                # Use 'match' key (new format)
                component_path = first_item.get("match", default_path)
            else:
                component_path = str(first_item)
        else:
            component_path = default_path

        if component_path != default_path:
            logger.info(f"Found path '{component_path}' for component '{component_name}'")
        else:
            logger.debug(f"No path found for component '{component_name}', using default '{default_path}'")

        return component_path

    def extract_component_paths(self, project_data: dict[str, Any], component_name: str) -> list[dict[str, str | None]]:
        """
        Extract publication paths from a component definition.

        Supports both simple string format and list format with optional rewrite.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find paths for

        Returns:
            List of path configs: [{"match": "/api", "rewrite": None}, ...]

        Examples:
            # Simple string format
            path: "/"
            -> [{"match": "/", "rewrite": None}]

            # List format with rewrite
            path:
              - match: "/kader"
                rewrite: "/"
            -> [{"match": "/kader", "rewrite": "/"}]
        """
        json_path = f"$.components[?(@.name='{component_name}')].path"
        path_config = self.extract_value_by_path(project_data, json_path, "/")

        # Normalize to list format
        if isinstance(path_config, str):
            logger.debug(f"Found single path '{path_config}' for component '{component_name}'")
            return [{"match": path_config, "rewrite": None}]
        elif isinstance(path_config, list):
            result = []
            for p in path_config:
                if isinstance(p, dict):
                    rewrite = p.get("rewrite")
                    match_value = p.get("match", "/")
                    result.append({"match": match_value, "rewrite": rewrite})
                else:
                    result.append({"match": str(p), "rewrite": None})
            logger.info(f"Found {len(result)} path(s) for component '{component_name}'")
            return result

        logger.debug(f"No path found for component '{component_name}', using default '/'")
        return [{"match": "/", "rewrite": None}]

    def extract_deployment_component_paths(
        self,
        project_data: dict[str, Any],
        deployment_data: dict[str, Any],
        component_reference: str,
    ) -> list[dict[str, str | None]]:
        """
        Extract publication paths for a component in a deployment context.

        Checks deployment-level paths first (deployments[].components[].paths),
        then falls back to component-level path (components[].path).

        Args:
            project_data: The parsed project data
            deployment_data: The specific deployment dict
            component_reference: Name of the component to find paths for

        Returns:
            List of path configs: [{"match": "/api", "rewrite": None}, ...]
        """
        # Check deployment-level paths first
        components = deployment_data.get("components", [])
        for comp in components:
            if comp.get("reference") == component_reference:
                paths = comp.get("paths")
                if paths is not None:
                    result = []
                    for p in paths:
                        if isinstance(p, dict):
                            result.append({"match": p.get("match", "/"), "rewrite": p.get("rewrite")})
                        else:
                            result.append({"match": str(p), "rewrite": None})
                    if result:
                        logger.info(
                            f"Found {len(result)} deployment-level path(s) for component '{component_reference}'"
                        )
                        return result
                break

        # Fall back to component-level paths
        return self.extract_component_paths(project_data, component_reference)

    def extract_component_storage(self, project_data: dict[str, Any], component_name: str) -> list[dict[str, Any]]:
        """
        Extract storage configuration from a component definition by name.

        Reads from the v2 format where storage lives under service entries
        in the component's ``services`` list (e.g. ``persistent-storage``
        and ``temp-storage`` dict entries with ``config`` lists).

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find storage for

        Returns:
            List of storage configurations or empty list if no storage found.
            Each dict has keys: name, size, mount-path, type.
        """
        component = self._find_component(project_data, component_name)
        if not component:
            logger.debug(f"Component '{component_name}' not found")
            return []

        storage_configs = extract_storage_from_component_services(component)
        if storage_configs:
            logger.info(f"Found {len(storage_configs)} storage config(s) for component '{component_name}'")
        else:
            logger.debug(f"No storage configuration found for component '{component_name}'")
        return storage_configs

    def _find_component(self, project_data: dict[str, Any], component_name: str) -> dict[str, Any] | None:
        """Find a component dict by name in project data."""
        for comp in project_data.get("components", []):
            if isinstance(comp, dict) and comp.get("name") == component_name:
                return comp
        return None

    def _decrypt_and_clean_env_vars(self, env_vars: dict[str, Any], private_key: str | None) -> dict[str, str]:
        """Decrypt and clean up individual env var values.

        Each value is run through the smart decrypt (handling plain, age-encrypted,
        or empty values) and then stripped of surrounding quotes.
        """
        cleaned: dict[str, str] = {}
        for key, value in env_vars.items():
            value_str = str(value) if value is not None else ""
            normalized_value = self._normalize_age_content(value_str)
            value_str = decrypt_password_smart_sync(normalized_value, private_key)

            # Strip surrounding quotes (single or double) if they wrap the whole value
            if value_str == '""' or value_str == "''":
                cleaned[key] = ""
            elif len(value_str) >= 2 and (
                (value_str.startswith('"') and value_str.endswith('"') and value_str.count('"') == 2)
                or (value_str.startswith("'") and value_str.endswith("'") and value_str.count("'") == 2)
            ):
                cleaned[key] = value_str[1:-1]
            else:
                cleaned[key] = value_str
        return cleaned

    async def _extract_user_env_vars(self, project_data: dict[str, Any], jsonpath: str, label: str) -> dict[str, str]:
        """Extract, decrypt, and clean user env vars from a JSONPath location.

        Args:
            project_data: The parsed project data
            jsonpath: JSONPath expression pointing to the user-env-vars field
            label: Human-readable label for log messages

        Returns:
            Dictionary of user environment variables or empty dict if none found
        """
        private_key = await get_decoded_project_private_key(project_data)

        raw = self.extract_value_by_path(project_data, jsonpath, {}, private_key)
        env_vars = validate_and_parse_env_vars(raw)

        if not env_vars:
            logger.debug(f"No user environment variables found for {label}")
            return {}

        logger.info(f"Found {len(env_vars)} user environment variable(s) for {label}")
        return self._decrypt_and_clean_env_vars(env_vars, private_key)

    async def extract_component_user_env_vars(
        self, project_data: dict[str, Any], component_name: str
    ) -> dict[str, str]:
        """Extract user environment variables from a component definition by name."""
        path = f"$.components[?(@.name='{component_name}')].user-env-vars"
        return await self._extract_user_env_vars(project_data, path, f"component '{component_name}'")

    async def extract_deployment_component_user_env_vars(
        self, project_data: dict[str, Any], deployment_name: str, component_reference: str
    ) -> dict[str, str]:
        """Extract user environment variables from a deployment component."""
        path = (
            f"$.deployments[?(@.name=='{deployment_name}')]"
            f".components[?(@.reference=='{component_reference}')].user-env-vars"
        )
        return await self._extract_user_env_vars(
            project_data, path, f"component '{component_reference}' in deployment '{deployment_name}'"
        )

    def get_persistent_storage(self, storage_configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Filter storage configurations to get only persistent storage items.

        Args:
            storage_configs: List of storage configurations

        Returns:
            List of persistent storage configurations
        """
        persistent_storage = [storage for storage in storage_configs if storage.get("type") == "persistent"]
        logger.debug(f"Found {len(persistent_storage)} persistent storage configurations")
        return persistent_storage

    def get_ephemeral_storage(self, storage_configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Filter storage configurations to get only ephemeral storage items.

        Args:
            storage_configs: List of storage configurations

        Returns:
            List of ephemeral storage configurations
        """
        ephemeral_storage = [storage for storage in storage_configs if storage.get("type") == "ephemeral"]
        logger.debug(f"Found {len(ephemeral_storage)} ephemeral storage configurations")
        return ephemeral_storage

    def extract_component_publish_on_web(self, project_data: dict[str, Any], component_name: str) -> bool:
        """
        Check if a component has the publish-on-web service enabled.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find

        Returns:
            True if publish-on-web service is in the component's services list, False otherwise
        """
        component = self._find_component(project_data, component_name)
        if not component:
            logger.debug(f"Component '{component_name}' not found for publish-on-web check")
            return False

        service_names = extract_service_names_from_component(component)
        component_services = ServiceAdapter.parse_services_from_strings(service_names)
        has_publish_service = ServiceType.PUBLISH_ON_WEB in component_services

        logger.debug(f"Component '{component_name}' has publish-on-web service: {has_publish_service}")
        return has_publish_service

    # ========================================================================
    # Resource Configuration Methods
    # ========================================================================

    def extract_component_resources(self, project_data: dict[str, Any], component_name: str) -> dict[str, str]:
        """
        Extract resource configuration from a component definition by name.

        Returns flat dict with keys: requests_memory, requests_cpu, limits_memory, limits_cpu.
        Missing values are filled with defaults.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find resources for

        Returns:
            Flat dictionary with resource values (always complete with defaults)
        """
        resources_path = f"$.components[?(@.name='{component_name}')].resources"
        raw_resources = self.extract_value_by_path(project_data, resources_path, None)

        return _parse_resources_block(raw_resources)

    def extract_deployment_component_resources(
        self, project_data: dict[str, Any], deployment_name: str, component_reference: str
    ) -> dict[str, str] | None:
        """
        Extract resource configuration from a deployment-level component override.

        Returns None if no deployment-level resources are defined (no override).
        Returns flat dict with partial or full overrides when present.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_reference: Reference name of the component

        Returns:
            Flat dictionary with resource overrides, or None if absent
        """
        # Navigate manually for nested array filter
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") != deployment_name:
                continue
            components = deployment.get("components", [])
            for comp in components:
                if comp.get("reference") != component_reference:
                    continue
                raw_resources = comp.get("resources")
                if raw_resources is None:
                    return None
                # Parse without defaults - return only what's explicitly set
                return _parse_resources_block_partial(raw_resources)
        return None

    def extract_component_disabled(self, project_data: dict[str, Any], component_name: str) -> tuple[bool, str]:
        """
        Extract disabled state from a component definition.

        Args:
            project_data: The parsed project data
            component_name: Name of the component

        Returns:
            Tuple of (disabled, reason)
        """
        disabled_path = f"$.components[?(@.name='{component_name}')].disabled"
        disabled = self.extract_value_by_path(project_data, disabled_path, False)

        reason_path = f"$.components[?(@.name='{component_name}')].disabled-reason"
        reason = self.extract_value_by_path(project_data, reason_path, "")

        return bool(disabled), str(reason or "")

    def set_component_disabled(
        self, project_data: dict[str, Any], component_name: str, disabled: bool, reason: str
    ) -> bool:
        """
        Set the disabled state on a component in the project data.

        Modifies project_data in place.

        Args:
            project_data: The parsed project data (modified in place)
            component_name: Name of the component
            disabled: Whether to disable the component
            reason: Reason for disabling

        Returns:
            True if the component was found and updated, False otherwise
        """
        components = project_data.get("components", [])
        for comp in components:
            if comp.get("name") == component_name:
                comp["disabled"] = disabled
                if disabled:
                    comp["disabled-reason"] = reason
                elif "disabled-reason" in comp:
                    del comp["disabled-reason"]
                logger.info(
                    f"Set component '{component_name}' disabled={disabled}"
                    + (f" reason='{reason}'" if disabled else "")
                )
                return True
        logger.warning(f"Component '{component_name}' not found for disabled state update")
        return False

    def set_component_resources(
        self, project_data: dict[str, Any], component_name: str, resources: dict[str, str]
    ) -> bool:
        """
        Set resource configuration on a component in the project data.

        Modifies project_data in place. The resources dict uses flat keys
        (requests_memory, requests_cpu, limits_memory, limits_cpu).

        Returns:
            True if the component was found and updated, False otherwise
        """
        components = project_data.get("components", [])
        for comp in components:
            if comp.get("name") == component_name:
                _apply_flat_resources(comp, resources)
                logger.info(f"Updated resources for component '{component_name}': {resources}")
                return True
        logger.warning(f"Component '{component_name}' not found for resource update")
        return False

    def set_deployment_component_resources(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_reference: str,
        resources: dict[str, str],
    ) -> bool:
        """
        Set resource configuration on a component within a specific deployment.

        Modifies project_data in place. The resources dict uses flat keys
        (requests_memory, requests_cpu, limits_memory, limits_cpu).

        Returns:
            True if the deployment component was found and updated, False otherwise
        """
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") != deployment_name:
                continue
            components = deployment.get("components", [])
            for comp in components:
                if comp.get("reference") != component_reference:
                    continue
                _apply_flat_resources(comp, resources)
                logger.info(
                    f"Updated resources for component '{component_reference}' "
                    f"in deployment '{deployment_name}': {resources}"
                )
                return True
        logger.warning(
            f"Component '{component_reference}' not found in deployment '{deployment_name}' for resource update"
        )
        return False

    def append_component_resource_history(
        self,
        project_data: dict[str, Any],
        component_name: str,
        entry: dict[str, Any],
        max_entries: int = 5,
    ) -> bool:
        """
        Append a resource history entry to a component definition.

        Inserts at the front (newest first) and prunes to max_entries.

        Args:
            project_data: The parsed project data (modified in place)
            component_name: Name of the component
            entry: History entry dict with timestamp, limits, source, reason
            max_entries: Maximum history entries to keep

        Returns:
            True if the component was found and updated
        """
        components = project_data.get("components", [])
        for comp in components:
            if comp.get("name") == component_name:
                if "resources" not in comp:
                    comp["resources"] = {}
                history = comp["resources"].get("history", [])
                history.insert(0, entry)
                comp["resources"]["history"] = history[:max_entries]
                return True
        return False

    def append_deployment_component_resource_history(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_reference: str,
        entry: dict[str, Any],
        max_entries: int = 5,
    ) -> bool:
        """
        Append a resource history entry to a deployment-level component override.

        Inserts at the front (newest first) and prunes to max_entries.

        Args:
            project_data: The parsed project data (modified in place)
            deployment_name: Name of the deployment
            component_reference: Reference name of the component
            entry: History entry dict with timestamp, limits, source, reason
            max_entries: Maximum history entries to keep

        Returns:
            True if the deployment component was found and updated
        """
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") != deployment_name:
                continue
            components = deployment.get("components", [])
            for comp in components:
                if comp.get("reference") != component_reference:
                    continue
                if "resources" not in comp:
                    comp["resources"] = {}
                history = comp["resources"].get("history", [])
                history.insert(0, entry)
                comp["resources"]["history"] = history[:max_entries]
                return True
        return False

    def get_resource_history_floor(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_reference: str,
    ) -> float | None:
        """
        Get the OOM watcher memory floor from resource history.

        Checks both deployment-component and component-definition history.
        Returns the highest OOM watcher limit found in the most recent
        oom-watcher entry at either level.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_reference: Reference name of the component

        Returns:
            Floor value in MB, or None if no OOM watcher entries exist
        """
        floor_mb: float | None = None

        # Check deployment-component history
        for dep in project_data.get("deployments", []):
            if dep.get("name") != deployment_name:
                continue
            for comp in dep.get("components", []):
                if comp.get("reference") != component_reference:
                    continue
                for entry in comp.get("resources", {}).get("history", []):
                    if entry.get("source") == "oom-watcher":
                        value = entry.get("limits", {}).get("memory")
                        if value:
                            mb = _k8s_memory_to_mb(value)
                            floor_mb = max(floor_mb or 0, mb)
                        break  # only check most recent oom-watcher entry

        # Check component-definition history
        for comp in project_data.get("components", []):
            if comp.get("name") != component_reference:
                continue
            for entry in comp.get("resources", {}).get("history", []):
                if entry.get("source") == "oom-watcher":
                    value = entry.get("limits", {}).get("memory")
                    if value:
                        mb = _k8s_memory_to_mb(value)
                        floor_mb = max(floor_mb or 0, mb)
                    break

        return floor_mb

    def extract_deployment_component_disabled(
        self, project_data: dict[str, Any], deployment_name: str, component_reference: str
    ) -> tuple[bool, str]:
        """
        Extract disabled state from a component within a specific deployment.

        Falls back to component-definition-level disabled state if no deployment-level
        override exists.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_reference: Reference name of the component

        Returns:
            Tuple of (disabled, reason)
        """
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") != deployment_name:
                continue
            components = deployment.get("components", [])
            for comp in components:
                if comp.get("reference") != component_reference:
                    continue
                if "disabled" in comp:
                    return bool(comp.get("disabled", False)), str(comp.get("disabled-reason", ""))

        # Fall back to component-definition-level disabled state
        return self.extract_component_disabled(project_data, component_reference)

    def set_deployment_component_disabled(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_reference: str,
        disabled: bool,
        reason: str,
    ) -> bool:
        """
        Set the disabled state on a component within a specific deployment.

        Modifies project_data in place.

        Args:
            project_data: The parsed project data (modified in place)
            deployment_name: Name of the deployment
            component_reference: Reference name of the component
            disabled: Whether to disable the component
            reason: Reason for disabling

        Returns:
            True if the deployment component was found and updated, False otherwise
        """
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") != deployment_name:
                continue
            components = deployment.get("components", [])
            for comp in components:
                if comp.get("reference") != component_reference:
                    continue
                comp["disabled"] = disabled
                if disabled:
                    comp["disabled-reason"] = reason
                elif "disabled-reason" in comp:
                    del comp["disabled-reason"]
                logger.info(
                    f"Set component '{component_reference}' in deployment '{deployment_name}' "
                    f"disabled={disabled}" + (f" reason='{reason}'" if disabled else "")
                )
                return True
        logger.warning(
            f"Component '{component_reference}' not found in deployment '{deployment_name}' for disabled state update"
        )
        return False

    # ========================================================================
    # Service Config Generation Methods (reference/config pattern)
    # ========================================================================

    def _get_service_config_generation(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_name: str,
        service_type: str,
        reference_name: str,
    ) -> int | None:
        """
        Get generation from a service using the reference/config pattern.

        Path: deployments[?(@.name=='{deployment_name}')].components[?(@.reference=='{component_name}')]
              .services.{service_type}[?(@.reference=='{reference_name}')].config.generation

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            service_type: Service type ("persistent-storage", "database", "minio-storage")
            reference_name: Reference name of the service item

        Returns:
            Generation number if set, None if not present (no version suffix will be used)
        """
        # Navigate manually since JSONPath with nested array filters can be tricky
        deployments = project_data.get("deployments", [])

        # Log available deployments for debugging
        deployment_names = [d.get("name") for d in deployments]
        logger.info(
            f"Looking for generation: deployment={deployment_name}, component={component_name}, "
            f"service_type={service_type}, reference={reference_name}. "
            f"Available deployments: {deployment_names}"
        )

        for deployment in deployments:
            if deployment.get("name") == deployment_name:
                components = deployment.get("components", [])
                component_refs = [c.get("reference") for c in components]
                logger.info(f"Found deployment '{deployment_name}', components: {component_refs}")

                for component in components:
                    if component.get("reference") == component_name:
                        services = component.get("services", {})
                        logger.info(f"Found component '{component_name}', services keys: {list(services.keys())}")
                        service_items = services.get(service_type, [])

                        # Handle list format (new pattern)
                        if isinstance(service_items, list):
                            item_refs = [i.get("reference") if isinstance(i, dict) else str(i) for i in service_items]
                            logger.info(f"Service items (list format) for '{service_type}': {item_refs}")

                            for item in service_items:
                                if isinstance(item, dict) and item.get("reference") == reference_name:
                                    config = item.get("config", {})
                                    generation = config.get("generation")
                                    logger.info(
                                        f"Found item '{reference_name}', config={config}, generation={generation}"
                                    )
                                    if generation is not None:
                                        logger.info(
                                            f"Returning generation {generation} for {deployment_name}/{component_name}/"
                                            f"{service_type}/{reference_name}"
                                        )
                                        return int(generation)
                                    logger.info(f"Generation is None for {reference_name}, returning None")
                                    return None

                        # Handle dict format (old pattern - backward compatibility)
                        elif isinstance(service_items, dict) and reference_name in service_items:
                            item_config = service_items[reference_name]
                            if isinstance(item_config, dict):
                                generation = item_config.get("generation")
                                if generation is not None:
                                    logger.debug(
                                        f"Service generation (old pattern) for {deployment_name}/"
                                        f"{component_name}/{service_type}/{reference_name}: {generation}"
                                    )
                                    return int(generation)
                                return None

        logger.info(f"No generation found for {deployment_name}/{component_name}/{service_type}/{reference_name}")
        return None

    def _set_service_config_generation(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_name: str,
        service_type: str,
        reference_name: str,
        generation: int,
    ) -> dict[str, Any]:
        """
        Set generation using the reference/config pattern.

        Creates the nested structure if it doesn't exist:
        deployments[name].components[reference==component_name].services.{service_type}
          = [{"reference": reference_name, "config": {"generation": generation}}]

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            service_type: Service type ("persistent-storage", "database", "minio-storage")
            reference_name: Reference name of the service item
            generation: Generation number to set

        Returns:
            Updated project_data dictionary
        """
        deployments = project_data.get("deployments", [])
        component_found = False

        for deployment in deployments:
            if deployment.get("name") == deployment_name:
                components = deployment.get("components", [])
                for component in components:
                    if component.get("reference") == component_name:
                        component_found = True

                        # Ensure 'services' dict exists
                        if "services" not in component:
                            component["services"] = {}

                        # Ensure service_type list exists
                        if service_type not in component["services"]:
                            component["services"][service_type] = []

                        service_items = component["services"][service_type]

                        # Handle list format (new pattern)
                        if isinstance(service_items, list):
                            # Find existing item or add new one
                            item_found = False
                            for item in service_items:
                                if isinstance(item, dict) and item.get("reference") == reference_name:
                                    # Ensure config dict exists
                                    if "config" not in item:
                                        item["config"] = {}
                                    item["config"]["generation"] = generation
                                    item_found = True
                                    break

                            if not item_found:
                                # Add new item with reference/config pattern
                                service_items.append(
                                    {"reference": reference_name, "config": {"generation": generation}}
                                )

                        # Handle dict format (old pattern) - convert to new pattern
                        elif isinstance(service_items, dict):
                            # Convert old dict pattern to new list pattern
                            new_list = []
                            for ref_name, ref_config in service_items.items():
                                if ref_name == reference_name:
                                    # Update this item with new generation
                                    new_list.append({"reference": ref_name, "config": {"generation": generation}})
                                elif isinstance(ref_config, dict) and "generation" in ref_config:
                                    # Preserve existing items
                                    new_list.append(
                                        {"reference": ref_name, "config": {"generation": ref_config["generation"]}}
                                    )
                                else:
                                    # Item without generation
                                    new_list.append({"reference": ref_name})

                            # If reference wasn't found in old dict, add it
                            if not any(item.get("reference") == reference_name for item in new_list):
                                new_list.append({"reference": reference_name, "config": {"generation": generation}})

                            component["services"][service_type] = new_list

                        logger.info(
                            f"Set service generation for {deployment_name}/{component_name}/"
                            f"{service_type}/{reference_name} to {generation}"
                        )
                        break

                if component_found:
                    break

        if not component_found:
            logger.warning(f"Component '{component_name}' in deployment '{deployment_name}' not found in project data")

        return project_data

    def get_storage_generation(
        self, project_data: dict[str, Any], deployment_name: str, component_name: str, storage_name: str
    ) -> int | None:
        """
        Get the current generation number for a storage in a deployment component.

        Uses the reference/config pattern:
        deployments[name].components[reference].services.persistent-storage[reference==storage_name].config.generation

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            storage_name: Name of the storage (e.g., "data", "temp")

        Returns:
            Generation number if set, None if not present (no version suffix will be used)
        """
        return self._get_service_config_generation(
            project_data, deployment_name, component_name, "persistent-storage", storage_name
        )

    def set_storage_generation(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_name: str,
        storage_name: str,
        generation: int,
    ) -> dict[str, Any]:
        """
        Set the generation number for a storage in a deployment component.

        Uses the reference/config pattern:
        deployments[name].components[reference].services.persistent-storage
          = [{"reference": storage_name, "config": {"generation": generation}}]

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            storage_name: Name of the storage (e.g., "data", "temp")
            generation: Generation number to set

        Returns:
            Updated project_data dictionary
        """
        return self._set_service_config_generation(
            project_data, deployment_name, component_name, "persistent-storage", storage_name, generation
        )

    def get_database_generation(
        self, project_data: dict[str, Any], deployment_name: str, component_name: str, reference_name: str
    ) -> int | None:
        """
        Get the current generation number for a database in a deployment component.

        Uses the reference/config pattern:
        deployments[name].components[reference].services.database[reference==reference_name].config.generation

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            reference_name: Reference name of the database

        Returns:
            Generation number if set, None if not present
        """
        return self._get_service_config_generation(
            project_data, deployment_name, component_name, ServiceType.POSTGRESQL_DATABASE.value, reference_name
        )

    def set_database_generation(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_name: str,
        reference_name: str,
        generation: int,
    ) -> dict[str, Any]:
        """
        Set the generation number for a database in a deployment component.

        Uses the reference/config pattern:
        deployments[name].components[reference].services.database
          = [{"reference": reference_name, "config": {"generation": generation}}]

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            reference_name: Reference name of the database
            generation: Generation number to set

        Returns:
            Updated project_data dictionary
        """
        return self._set_service_config_generation(
            project_data,
            deployment_name,
            component_name,
            ServiceType.POSTGRESQL_DATABASE.value,
            reference_name,
            generation,
        )

    def get_bucket_generation(
        self, project_data: dict[str, Any], deployment_name: str, component_name: str, reference_name: str
    ) -> int | None:
        """
        Get the current generation number for a bucket in a deployment component.

        Uses the reference/config pattern:
        deployments[name].components[reference].services.minio-storage[reference==reference_name].config.generation

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            reference_name: Reference name of the bucket

        Returns:
            Generation number if set, None if not present
        """
        return self._get_service_config_generation(
            project_data, deployment_name, component_name, "minio-storage", reference_name
        )

    def set_bucket_generation(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_name: str,
        reference_name: str,
        generation: int,
    ) -> dict[str, Any]:
        """
        Set the generation number for a bucket in a deployment component.

        Uses the reference/config pattern:
        deployments[name].components[reference].services.minio-storage
          = [{"reference": reference_name, "config": {"generation": generation}}]

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_name: Name of the component
            reference_name: Reference name of the bucket
            generation: Generation number to set

        Returns:
            Updated project_data dictionary
        """
        return self._set_service_config_generation(
            project_data, deployment_name, component_name, "minio-storage", reference_name, generation
        )

    # ========================================================================
    # Deployment-Level Service Generation Methods
    # ========================================================================
    # These methods store/retrieve generation at the deployment level for
    # services like database and minio that are deployment-wide resources.
    # Structure: deployments[name].services.{service_type}.generation
    #
    # Callers should use ServiceType enum values for service_type parameter:
    # - ServiceType.POSTGRESQL_DATABASE.value ("postgresql-database")
    # - ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value ("namespace-postgresql-database")
    # - ServiceType.MINIO_STORAGE.value ("minio-storage")

    def get_deployment_service_generation(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str
    ) -> int | None:
        """
        Get generation from deployment-level services block.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)

        Returns:
            Generation number if set, None if not present

        Expected format:
        deployments:
          - name: deployment-1
            services:
              - reference: postgresql-database
                config:
                  generation: 1
              - reference: minio-storage
                config:
                  generation: 2
        """
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") == deployment_name:
                services = deployment.get("services", [])

                # Services is a list of {reference: ..., config: ...}
                if isinstance(services, list):
                    for item in services:
                        if isinstance(item, dict) and item.get("reference") == service_type:
                            config = item.get("config", {})
                            generation = config.get("generation")
                            if generation is not None:
                                logger.debug(
                                    f"Deployment service generation for {deployment_name}/{service_type}: {generation}"
                                )
                                return int(generation)
                            return None

                return None
        return None

    def set_deployment_service_generation(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str, generation: int
    ) -> dict[str, Any]:
        """
        Set generation in deployment-level services block.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            service_type: Service type (use ServiceType enum values)
            generation: Generation number to set

        Returns:
            Updated project_data dictionary

        Format:
        deployments:
          - name: deployment-1
            services:
              - reference: postgresql-database
                config:
                  generation: 1
        """
        deployments = project_data.get("deployments", [])
        deployment_found = False

        for deployment in deployments:
            if deployment.get("name") == deployment_name:
                deployment_found = True

                # Ensure services list exists
                if "services" not in deployment:
                    deployment["services"] = []
                services = deployment["services"]

                # Convert dict to list if needed (migration from old format)
                if isinstance(services, dict):
                    new_list = []
                    for ref_name, ref_config in services.items():
                        if isinstance(ref_config, dict) and "generation" in ref_config:
                            new_list.append({"reference": ref_name, "config": {"generation": ref_config["generation"]}})
                        else:
                            new_list.append({"reference": ref_name})
                    deployment["services"] = new_list
                    services = deployment["services"]

                # Find existing service entry or create new one
                service_entry = None
                for item in services:
                    if isinstance(item, dict) and item.get("reference") == service_type:
                        service_entry = item
                        break

                if service_entry is None:
                    service_entry = {"reference": service_type, "config": {"generation": generation}}
                    services.append(service_entry)
                else:
                    if "config" not in service_entry or not isinstance(service_entry["config"], dict):
                        service_entry["config"] = {}
                    service_entry["config"]["generation"] = generation

                logger.info(f"Set deployment service generation for {deployment_name}/{service_type} to {generation}")
                break

        if not deployment_found:
            logger.warning(f"Deployment '{deployment_name}' not found in project data")

        return project_data

    def extract_backup_config(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract project-level backup configuration.

        Args:
            project_data: The parsed project data

        Returns:
            Backup configuration dict with keys:
            - enabled: bool (default False)
            - schedule: str (daily, weekly, manual) (default manual)
        """
        backup_config = project_data.get("backup", {})
        return {
            "enabled": backup_config.get("enabled", False),
            "schedule": backup_config.get("schedule", "manual"),
        }

    def get_storage_backup_enabled(self, project_data: dict[str, Any], component_name: str, storage_name: str) -> bool:
        """
        Check if backup is enabled for a specific storage in a component.

        The backup setting is determined by:
        1. Per-storage override in component storage config (storage[].backup)
        2. Project-level backup.enabled setting

        Args:
            project_data: The parsed project data
            component_name: Name of the component
            storage_name: Name of the storage (e.g., "data", "cache")

        Returns:
            True if backup should be enabled for this storage
        """
        # First check project-level backup setting
        backup_config = self.extract_backup_config(project_data)
        project_backup_enabled = backup_config.get("enabled", False)

        # Then check per-storage override - look in component services for storage config
        storage_backup = None
        component = self._find_component(project_data, component_name)
        if component:
            for item in extract_storage_from_component_services(component):
                if item.get("name") == storage_name and "backup" in item:
                    storage_backup = item["backup"]
                    break

        # Per-storage setting overrides project setting if specified
        if storage_backup is not None:
            logger.debug(f"Storage {component_name}/{storage_name} backup override: {storage_backup}")
            return bool(storage_backup)

        logger.debug(f"Storage {component_name}/{storage_name} using project backup setting: {project_backup_enabled}")
        return project_backup_enabled

    def extract_remote_sources(self, project_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract remote-sources from project data.

        Args:
            project_data: The parsed project data

        Returns:
            List of remote source configurations
        """
        remote_sources = project_data.get("remote-sources", [])
        logger.debug(f"Found {len(remote_sources)} remote source(s)")
        return remote_sources

    def get_remote_source_by_name(self, project_data: dict[str, Any], name: str) -> dict[str, Any] | None:
        """
        Get a specific remote source by name.

        Args:
            project_data: The parsed project data
            name: Name of the remote source to find

        Returns:
            Remote source configuration or None if not found
        """
        remote_sources = self.extract_remote_sources(project_data)
        for source in remote_sources:
            if source.get("name") == name:
                logger.debug(f"Found remote source: {name}")
                return source

        logger.warning(f"Remote source '{name}' not found")
        return None

    def extract_deployment_clone_from_config(
        self, project_data: dict[str, Any], deployment_name: str
    ) -> dict[str, Any] | None:
        """
        Extract clone-from configuration from a deployment.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment to extract clone config from

        Returns:
            Clone configuration dict with keys:
            - type: "deployment" | "remote-source"
            - reference: deployment name or remote source name
            - mode: "once" | "always" | "manual"
            - services: optional list of services to clone
            Returns None if no clone-from configuration exists
        """
        path = f"$.deployments[?(@.name=='{deployment_name}')].clone-from"
        clone_from_config = self.extract_value_by_path(project_data, path, None)

        if clone_from_config:
            logger.info(
                f"Found clone-from config for {deployment_name}: "
                f"type={clone_from_config.get('type')}, "
                f"reference={clone_from_config.get('reference')}, "
                f"mode={clone_from_config.get('mode')}"
            )
        else:
            logger.debug(f"No clone-from configuration for deployment: {deployment_name}")

        return clone_from_config

    def get_clone_status(self, project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
        """
        Get the clone status from a deployment's clone-from configuration.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            Status dict with keys:
            - completed: bool
            - timestamp: str (ISO format)
            Returns None if no status exists
        """
        clone_from = self.extract_deployment_clone_from_config(project_data, deployment_name)
        if clone_from and isinstance(clone_from, dict):
            status = clone_from.get("status")
            if status:
                logger.debug(f"Found clone status for {deployment_name}: completed={status.get('completed')}")
                return status
        return None

    def set_clone_status(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        completed: bool,
        timestamp: str,
    ) -> dict[str, Any]:
        """
        Set the clone status in a deployment's clone-from configuration.

        This adds/updates the status field in the clone-from block:
        clone-from:
          type: remote-source
          reference: production-db
          mode: once
          status:
            completed: true
            timestamp: "2026-01-16T16:23:45Z"

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            completed: Whether the clone completed successfully
            timestamp: ISO format timestamp of when the clone was performed

        Returns:
            Updated project_data dictionary
        """
        deployments = project_data.get("deployments", [])

        for deployment in deployments:
            if deployment.get("name") == deployment_name:
                clone_from = deployment.get("clone-from")
                if not clone_from:
                    raise ValueError(
                        f"Cannot set clone status for '{deployment_name}': no clone-from configuration found"
                    )
                if not isinstance(clone_from, dict):
                    raise ValueError(
                        f"Cannot set clone status for '{deployment_name}': clone-from must be a dict "
                        f"with 'type', 'reference', and 'mode' keys, got: {type(clone_from).__name__} = {clone_from!r}"
                    )
                clone_from["status"] = {
                    "completed": completed,
                    "timestamp": timestamp,
                }
                logger.info(f"Set clone status for {deployment_name}: completed={completed}, timestamp={timestamp}")
                break

        return project_data

    def get_clone_mode(self, project_data: dict[str, Any], deployment_name: str) -> str:
        """
        Get the clone mode from a deployment's clone-from configuration.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            Clone mode: "once" (default) or "always"
        """
        clone_from = self.extract_deployment_clone_from_config(project_data, deployment_name)
        if clone_from and isinstance(clone_from, dict):
            mode = clone_from.get("mode", "once")
            logger.debug(f"Clone mode for {deployment_name}: {mode}")
            return mode
        return "once"

    def is_clone_completed(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """
        Check if a clone has been completed for a deployment.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            True if status.completed is True, False otherwise
        """
        status = self.get_clone_status(project_data, deployment_name)
        if status:
            return status.get("completed", False)
        return False

    def extract_registries(self, project_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract container registry configurations from project file.

        Args:
            project_data: The parsed project data

        Returns:
            List of registry configuration dicts with keys: name, url, and either
            (username, password) for credential-based auth, or secretName for
            pre-existing Kubernetes secrets. URL may include paths (e.g., rcr.rijksapps.nl/rig).
        """
        registries_path = "$.registries[*]"
        registries = self.extract_value_by_path(project_data, registries_path, [])

        if isinstance(registries, list) and registries:
            logger.info(f"Found {len(registries)} container registr{'y' if len(registries) == 1 else 'ies'}")
            return registries

        logger.debug("No container registries configured in project file")
        return []

    def extract_component_registry(self, project_data: dict[str, Any], component_name: str) -> dict[str, Any] | None:
        """
        Extract registry configuration for a component by resolving its registry reference.

        Args:
            project_data: The parsed project data
            component_name: Name of the component

        Returns:
            Registry config dict with keys: name, url, username, password
            or None if component has no registry configured
        """
        # Check if component has registry reference
        path = f"$.components[?(@.name=='{component_name}')].registry"
        registry_ref = self.extract_value_by_path(project_data, path, None)

        if not registry_ref:
            return None

        # Find registry by name in registries list
        registries = self.extract_registries(project_data)
        for registry in registries:
            if registry.get("name") == registry_ref:
                logger.info(f"Component '{component_name}' uses registry '{registry_ref}'")
                return registry

        logger.warning(f"Component '{component_name}' references registry '{registry_ref}' which does not exist")
        return None

    def extract_helm_charts(self, project_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract helm-charts definitions from project data.

        Args:
            project_data: The parsed project data

        Returns:
            List of helm-chart configurations
        """
        helm_charts = project_data.get("helm-charts", [])
        logger.debug(f"Found {len(helm_charts)} helm chart(s)")
        return helm_charts

    def get_helm_chart_by_name(self, project_data: dict[str, Any], name: str) -> dict[str, Any] | None:
        """
        Get a specific helm-chart by name.

        Args:
            project_data: The parsed project data
            name: Name of the helm-chart to find

        Returns:
            Helm-chart configuration or None if not found
        """
        helm_charts = self.extract_helm_charts(project_data)
        for chart in helm_charts:
            if chart.get("name") == name:
                logger.debug(f"Found helm chart: {name}")
                return chart

        logger.warning(f"Helm chart '{name}' not found")
        return None

    async def extract_helm_chart_values(self, project_data: dict[str, Any], chart_name: str) -> dict[str, Any]:
        """
        Extract helm-values from a helm-chart definition by name (base values).

        The values are AGE-encrypted in the project file and are decrypted here.

        Args:
            project_data: The parsed project data
            chart_name: Name of the helm-chart to extract values for

        Returns:
            Dictionary of helm values or empty dict if none found
        """
        private_key = await get_decoded_project_private_key(project_data)

        # Use JSONPath to find the helm-chart by name and extract its helm-values
        path = f"$.helm-charts[?(@.name=='{chart_name}')].helm-values"
        helm_values_str = self.extract_value_by_path(project_data, path, None, private_key)

        if helm_values_str and isinstance(helm_values_str, dict):
            logger.info(f"Found {len(helm_values_str)} helm value(s) for chart '{chart_name}'")
            return helm_values_str
        elif helm_values_str and isinstance(helm_values_str, str):
            # Try parsing as YAML if it's a string
            try:
                yaml = YAML()
                parsed_values = yaml.load(helm_values_str)
                if isinstance(parsed_values, dict):
                    logger.info(f"Parsed helm values for chart '{chart_name}'")
                    return parsed_values
            except Exception as e:
                logger.warning(f"Could not parse helm values as YAML for chart '{chart_name}': {e}")

        logger.debug(f"No helm-values found for chart '{chart_name}'")
        return {}

    async def extract_deployment_helm_chart_values(
        self, project_data: dict[str, Any], deployment_name: str, chart_reference: str
    ) -> dict[str, Any]:
        """
        Extract helm-values from a deployment's helm-chart reference (deployment-specific overrides).

        The values are AGE-encrypted in the project file and are decrypted here.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            chart_reference: Reference name of the helm-chart in the deployment

        Returns:
            Dictionary of helm values or empty dict if none found
        """
        private_key = await get_decoded_project_private_key(project_data)

        # Use JSONPath to find the deployment helm-chart reference and extract its helm-values
        path = (
            f"$.deployments[?(@.name=='{deployment_name}')]"
            f".helm-charts[?(@.reference=='{chart_reference}')].helm-values"
        )
        helm_values_str = self.extract_value_by_path(project_data, path, None, private_key)

        if helm_values_str and isinstance(helm_values_str, dict):
            logger.info(
                f"Found {len(helm_values_str)} deployment-level helm value(s) "
                f"for chart '{chart_reference}' in deployment '{deployment_name}'"
            )
            return helm_values_str
        elif helm_values_str and isinstance(helm_values_str, str):
            # Try parsing as YAML if it's a string
            try:
                yaml = YAML()
                parsed_values = yaml.load(helm_values_str)
                if isinstance(parsed_values, dict):
                    logger.info(
                        f"Parsed deployment-level helm values for chart '{chart_reference}' "
                        f"in deployment '{deployment_name}'"
                    )
                    return parsed_values
            except Exception as e:
                logger.warning(
                    f"Could not parse helm values as YAML for chart '{chart_reference}' "
                    f"in deployment '{deployment_name}': {e}"
                )

        logger.debug(
            f"No deployment-level helm-values found for chart '{chart_reference}' in deployment '{deployment_name}'"
        )
        return {}

    def extract_deployment_namespace(self, project_data: dict[str, Any], deployment_name: str) -> str | None:
        """
        Extract the raw namespace for a deployment.

        Note: This returns the raw namespace from the project file. To get the actual
        Kubernetes namespace, use get_prefixed_namespace(cluster, namespace) from
        opi.core.cluster_config.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            Raw namespace string if found, None otherwise
        """
        path = f"$.deployments[?(@.name=='{deployment_name}')].namespace"
        namespace = self.extract_value_by_path(project_data, path, None)

        if namespace:
            logger.debug(f"Found namespace '{namespace}' for deployment '{deployment_name}'")
            return namespace

        logger.warning(f"No namespace found for deployment '{deployment_name}'")
        return None

    def extract_deployment_cluster(self, project_data: dict[str, Any], deployment_name: str) -> str | None:
        """
        Extract the cluster for a deployment.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            Cluster string if found, None otherwise
        """
        path = f"$.deployments[?(@.name=='{deployment_name}')].cluster"
        cluster = self.extract_value_by_path(project_data, path, None)

        if cluster:
            logger.debug(f"Found cluster '{cluster}' for deployment '{deployment_name}'")
            return cluster

        logger.warning(f"No cluster found for deployment '{deployment_name}'")
        return None

    def extract_deployment_components(self, project_data: dict[str, Any], deployment_name: str) -> list[dict[str, Any]]:
        """
        Extract component references from a deployment.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            List of component references with their deployment-specific config
        """
        path = f"$.deployments[?(@.name=='{deployment_name}')].components"
        components = self.extract_value_by_path(project_data, path, [])

        if components:
            logger.debug(f"Found {len(components)} component reference(s) in deployment '{deployment_name}'")
            return components if isinstance(components, list) else [components]

        logger.debug(f"No components found in deployment '{deployment_name}'")
        return []

    def extract_deployment_helm_charts(
        self, project_data: dict[str, Any], deployment_name: str
    ) -> list[dict[str, Any]]:
        """
        Extract helm-chart references from a deployment.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            List of helm-chart references with their deployment-specific config
        """
        path = f"$.deployments[?(@.name=='{deployment_name}')].helm-charts"
        helm_charts = self.extract_value_by_path(project_data, path, [])

        if helm_charts:
            logger.info(f"Found {len(helm_charts)} helm chart reference(s) in deployment '{deployment_name}'")
            return helm_charts if isinstance(helm_charts, list) else [helm_charts]

        # Intentionally no log for missing helm-charts — most deployments don't have them
        return []

    def extract_helm_chart_uses_services(self, project_data: dict[str, Any], chart_name: str) -> list[str]:
        """
        Extract services from a helm-chart definition.

        Args:
            project_data: The parsed project data
            chart_name: Name of the helm-chart

        Returns:
            List of service names the helm chart uses
        """
        for chart in project_data.get("helm-charts", []):
            if isinstance(chart, dict) and chart.get("name") == chart_name:
                services = chart.get("services", [])
                if services:
                    service_names = ServiceAdapter.extract_service_names_from_project_services(services)
                    logger.info(f"Helm chart '{chart_name}' uses services: {service_names}")
                    return service_names
                break

        logger.debug(f"No services found for helm chart '{chart_name}'")
        return []

    # ========================================================================
    # Helmfile Methods
    # ========================================================================

    def extract_helmfiles(self, project_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract helmfile definitions from project data.

        Args:
            project_data: The parsed project data

        Returns:
            List of helmfile configurations
        """
        helmfiles = project_data.get("helmfile", [])
        logger.debug(f"Found {len(helmfiles)} helmfile(s)")
        return helmfiles

    def get_helmfile_by_name(self, project_data: dict[str, Any], name: str) -> dict[str, Any] | None:
        """
        Get a specific helmfile by name.

        Args:
            project_data: The parsed project data
            name: Name of the helmfile to find

        Returns:
            Helmfile configuration or None if not found
        """
        helmfiles = self.extract_helmfiles(project_data)
        for helmfile in helmfiles:
            if helmfile.get("name") == name:
                logger.debug(f"Found helmfile: {name}")
                return helmfile

        logger.warning(f"Helmfile '{name}' not found")
        return None

    async def extract_helmfile_values(self, project_data: dict[str, Any], helmfile_name: str) -> dict[str, Any]:
        """
        Extract helm-values from a helmfile definition by name (base values).

        The values are AGE-encrypted in the project file and are decrypted here.

        Args:
            project_data: The parsed project data
            helmfile_name: Name of the helmfile to extract values for

        Returns:
            Dictionary of helm values or empty dict if none found
        """
        private_key = await get_decoded_project_private_key(project_data)

        # Use JSONPath to find the helmfile by name and extract its helm-values
        path = f"$.helmfile[?(@.name=='{helmfile_name}')].helm-values"
        helm_values_str = self.extract_value_by_path(project_data, path, None, private_key)

        if helm_values_str and isinstance(helm_values_str, dict):
            logger.info(f"Found {len(helm_values_str)} helm value(s) for helmfile '{helmfile_name}'")
            return helm_values_str
        elif helm_values_str and isinstance(helm_values_str, str):
            # Try parsing as YAML if it's a string
            try:
                yaml = YAML()
                parsed_values = yaml.load(helm_values_str)
                if isinstance(parsed_values, dict):
                    logger.info(f"Parsed helm values for helmfile '{helmfile_name}'")
                    return parsed_values
            except Exception as e:
                logger.warning(f"Could not parse helm values as YAML for helmfile '{helmfile_name}': {e}")

        logger.debug(f"No helm-values found for helmfile '{helmfile_name}'")
        return {}

    async def extract_deployment_helmfile_values(
        self, project_data: dict[str, Any], deployment_name: str, helmfile_reference: str
    ) -> dict[str, Any]:
        """
        Extract helm-values from a deployment's helmfile reference (deployment-specific overrides).

        The values are AGE-encrypted in the project file and are decrypted here.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            helmfile_reference: Reference name of the helmfile in the deployment

        Returns:
            Dictionary of helm values or empty dict if none found
        """
        private_key = await get_decoded_project_private_key(project_data)

        # Use JSONPath to find the deployment helmfile reference and extract its helm-values
        path = (
            f"$.deployments[?(@.name=='{deployment_name}')]"
            f".helmfile[?(@.reference=='{helmfile_reference}')].helm-values"
        )
        helm_values_str = self.extract_value_by_path(project_data, path, None, private_key)

        if helm_values_str and isinstance(helm_values_str, dict):
            logger.info(
                f"Found {len(helm_values_str)} deployment-level helm value(s) "
                f"for helmfile '{helmfile_reference}' in deployment '{deployment_name}'"
            )
            return helm_values_str
        elif helm_values_str and isinstance(helm_values_str, str):
            # Try parsing as YAML if it's a string
            try:
                yaml = YAML()
                parsed_values = yaml.load(helm_values_str)
                if isinstance(parsed_values, dict):
                    logger.info(
                        f"Parsed deployment-level helm values for helmfile '{helmfile_reference}' "
                        f"in deployment '{deployment_name}'"
                    )
                    return parsed_values
            except Exception as e:
                logger.warning(
                    f"Could not parse helm values as YAML for helmfile '{helmfile_reference}' "
                    f"in deployment '{deployment_name}': {e}"
                )

        logger.debug(
            f"No deployment-level helm-values found for helmfile '{helmfile_reference}' "
            f"in deployment '{deployment_name}'"
        )
        return {}

    def extract_deployment_helmfiles(self, project_data: dict[str, Any], deployment_name: str) -> list[dict[str, Any]]:
        """
        Extract helmfile references from a deployment.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment

        Returns:
            List of helmfile references with their deployment-specific config
        """
        path = f"$.deployments[?(@.name=='{deployment_name}')].helmfile"
        helmfiles = self.extract_value_by_path(project_data, path, [])

        if helmfiles:
            logger.info(f"Found {len(helmfiles)} helmfile reference(s) in deployment '{deployment_name}'")
            return helmfiles if isinstance(helmfiles, list) else [helmfiles]

        # Intentionally no log for missing helmfiles — most deployments don't have them
        return []

    def extract_helmfile_uses_services(self, project_data: dict[str, Any], helmfile_name: str) -> list[str]:
        """
        Extract services from a helmfile definition.

        Args:
            project_data: The parsed project data
            helmfile_name: Name of the helmfile

        Returns:
            List of service names the helmfile uses
        """
        for hf in project_data.get("helmfile", []):
            if isinstance(hf, dict) and hf.get("name") == helmfile_name:
                services = hf.get("services", [])
                if services:
                    service_names = ServiceAdapter.extract_service_names_from_project_services(services)
                    logger.info(f"Helmfile '{helmfile_name}' uses services: {service_names}")
                    return service_names
                break

        logger.debug(f"No services found for helmfile '{helmfile_name}'")
        return []

    def deployment_uses_service(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_types: list[str],
    ) -> bool:
        """
        Check if a deployment uses any of the specified services.

        This checks both:
        1. Components referenced by the deployment and their services
        2. Helm-charts referenced by the deployment and their services

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment to check
            service_types: List of service type values to check for (e.g., ["postgresql-database", "namespace-postgresql-database"])

        Returns:
            True if deployment uses any of the specified services, False otherwise
        """
        # Check components
        component_refs = self.extract_deployment_components(project_data, deployment_name)
        for component_ref in component_refs:
            ref_name = component_ref.get("reference") if isinstance(component_ref, dict) else component_ref
            if ref_name:
                component = self._find_component(project_data, ref_name)
                if component:
                    service_names = extract_service_names_from_component(component)
                    if any(s in service_types for s in service_names):
                        return True

        # Check helm-charts
        helm_chart_refs = self.extract_deployment_helm_charts(project_data, deployment_name)
        for helm_chart_ref in helm_chart_refs:
            ref_name = helm_chart_ref.get("reference") if isinstance(helm_chart_ref, dict) else helm_chart_ref
            if ref_name:
                service_names = self.extract_helm_chart_uses_services(project_data, ref_name)
                if any(s in service_types for s in service_names):
                    return True

        # Check helmfiles
        helmfile_refs = self.extract_deployment_helmfiles(project_data, deployment_name)
        for helmfile_ref in helmfile_refs:
            ref_name = helmfile_ref.get("reference") if isinstance(helmfile_ref, dict) else helmfile_ref
            if ref_name:
                service_names = self.extract_helmfile_uses_services(project_data, ref_name)
                if any(s in service_types for s in service_names):
                    return True

        return False

    def get_components_using_service(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        service_types: list[str],
    ) -> list[dict[str, Any]]:
        """
        Get components in a deployment that use specified service types.

        Returns a list of dicts with component info including the service reference.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment to check
            service_types: List of service type values to check for

        Returns:
            List of dicts with keys: component_name, service_type, reference_name
        """
        results: list[dict[str, Any]] = []

        component_refs = self.extract_deployment_components(project_data, deployment_name)
        for component_ref in component_refs:
            ref_name = component_ref.get("reference") if isinstance(component_ref, dict) else component_ref
            if not ref_name:
                continue

            component = self._find_component(project_data, ref_name)
            if not component:
                continue

            service_list = component.get("services", [])
            for service in service_list:
                if isinstance(service, str):
                    service_name = service
                    service_ref = f"{deployment_name}-{service_name.split('-')[0]}"
                elif isinstance(service, dict):
                    service_name = next(iter(service.keys()))
                    service_config = service[service_name]
                    service_ref = service_config.get("reference", f"{deployment_name}-{service_name.split('-')[0]}")
                else:
                    continue

                if service_name in service_types:
                    results.append(
                        {
                            "component_name": ref_name,
                            "service_type": service_name,
                            "reference_name": service_ref,
                        }
                    )

        return results

    # ========================================================================
    # Invite System Methods
    # ========================================================================

    def extract_invites_config(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract the invites configuration section from project data.

        Args:
            project_data: The parsed project data

        Returns:
            Invites configuration dict with 'settings' and 'active' keys,
            or empty dict if no invites configured
        """
        invites = project_data.get("invites", {})
        if invites:
            logger.debug(f"Found invites config with {len(invites.get('active', []))} active invite(s)")
        else:
            logger.debug("No invites configuration found in project data")
        return invites

    def get_invite_settings(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract invite settings from project data.

        Args:
            project_data: The parsed project data

        Returns:
            Settings dict with defaults applied:
            - allow_sso: bool (default True)
            - allow_local: bool (default True)
            - default_expiration_days: int (default 7)
            - default_language: str (default 'nl')
        """
        invites = self.extract_invites_config(project_data)
        settings = invites.get("settings", {})

        return {
            "allow_sso": settings.get("allow_sso", True),
            "allow_local": settings.get("allow_local", True),
            "default_expiration_days": settings.get("default_expiration_days", 7),
            "default_language": settings.get("default_language", "nl"),
        }

    def get_invite_by_key(self, project_data: dict[str, Any], key: str) -> dict[str, Any] | None:
        """
        Get a specific invite by its key.

        Args:
            project_data: The parsed project data
            key: The invite key to search for

        Returns:
            Invite configuration dict or None if not found
        """
        invites = self.extract_invites_config(project_data)
        active_invites = invites.get("active", [])

        for invite in active_invites:
            if invite.get("key") == key:
                logger.debug(f"Found invite with key: {key}")
                return invite

        logger.debug(f"Invite with key '{key}' not found")
        return None

    def get_all_active_invites(self, project_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Get all active invites from project data.

        Args:
            project_data: The parsed project data

        Returns:
            List of active invite configurations
        """
        invites = self.extract_invites_config(project_data)
        active = invites.get("active", [])
        logger.debug(f"Found {len(active)} active invite(s)")
        return active

    def get_invite_auth_methods(self, project_data: dict[str, Any], invite: dict[str, Any]) -> dict[str, bool]:
        """
        Determine allowed authentication methods for an invite.

        Combines project-level settings with invite-specific overrides.

        Args:
            project_data: The parsed project data
            invite: The specific invite configuration

        Returns:
            Dict with 'sso' and 'local' boolean values
        """
        settings = self.get_invite_settings(project_data)

        # Check invite-specific auth_methods override
        invite_auth_methods = invite.get("auth_methods")

        if invite_auth_methods:
            # Invite specifies exact methods
            return {
                "sso": "sso" in invite_auth_methods,
                "local": "local" in invite_auth_methods,
            }

        # Fall back to project-level settings
        return {
            "sso": settings.get("allow_sso", True),
            "local": settings.get("allow_local", True),
        }

    def get_invite_message(self, invite: dict[str, Any], language: str = "nl") -> str:
        """
        Get the invite message in the specified language.

        Args:
            invite: The invite configuration
            language: Language code (default: 'nl')

        Returns:
            The invite message string
        """
        message = invite.get("message", "")

        if isinstance(message, dict):
            # Multi-language message
            return message.get(language, message.get("nl", message.get("en", "")))
        elif isinstance(message, str):
            # Simple string message
            return message

        return ""

    def get_invite_success_title(self, invite: dict[str, Any], language: str = "nl") -> str:
        """
        Get the success page title in the specified language.

        Args:
            invite: The invite configuration
            language: Language code (default: 'nl')

        Returns:
            The success title string or default
        """
        title = invite.get("success_title")

        if isinstance(title, dict):
            return title.get(language, title.get("nl", title.get("en", "")))
        elif isinstance(title, str):
            return title

        # Default titles
        defaults = {
            "nl": "Account aangemaakt",
            "en": "Account created",
        }
        return defaults.get(language, defaults["nl"])

    def get_invite_success_button(self, invite: dict[str, Any], language: str = "nl") -> str:
        """
        Get the success page button text in the specified language.

        Args:
            invite: The invite configuration
            language: Language code (default: 'nl')

        Returns:
            The button text string or default
        """
        button = invite.get("success_button")

        if isinstance(button, dict):
            return button.get(language, button.get("nl", button.get("en", "")))
        elif isinstance(button, str):
            return button

        # Default button texts
        defaults = {
            "nl": "Ga naar applicatie",
            "en": "Go to application",
        }
        return defaults.get(language, defaults["nl"])


def extract_storage_from_component_services(component: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract storage configurations from a v2 component's services list.

    In v2 format, storage is defined under service dict entries like::

        services:
        - persistent-storage:
            config:
            - name: data
              size: 250Mi
              mount-path: /data

    This function finds ``persistent-storage`` and ``temp-storage`` entries,
    extracts their config items, and adds back the ``type`` field for
    downstream compatibility (PVC manager, backup, etc.).

    Args:
        component: A component dict from project data

    Returns:
        List of storage config dicts with keys: name, size, mount-path, type
    """
    from opi.services.schema_migration import _STORAGE_SERVICE_TO_TYPE

    storage_configs: list[dict[str, Any]] = []
    services = component.get("services", [])

    for entry in services:
        if not isinstance(entry, dict):
            continue
        for service_name, service_data in entry.items():
            if service_name not in _STORAGE_SERVICE_TO_TYPE:
                continue
            storage_type = _STORAGE_SERVICE_TO_TYPE[service_name]
            config_items = service_data.get("config", []) if isinstance(service_data, dict) else []
            for item in config_items:
                if isinstance(item, dict):
                    config = dict(item)
                    config["type"] = storage_type
                    storage_configs.append(config)

    return storage_configs


def extract_service_names_from_component(component: dict[str, Any]) -> list[str]:
    """
    Extract service names from a component's services list.

    Supports both v2 format (``services`` key with mixed string/dict entries)
    and v1 format (``uses-services`` key with plain string entries).
    When both keys exist, results are merged and deduplicated.

    Dict entries with ``None`` or empty values (e.g. ``{"persistent-storage": null}``)
    are skipped - these are unconfigured placeholders left by the form system.

    Args:
        component: A component dict from project data

    Returns:
        List of service name strings
    """
    v2_services = _filter_empty_service_entries(component.get("services", []))
    v1_services = component.get("uses-services", [])

    names = ServiceAdapter.extract_service_names_from_project_services(v2_services)

    if v1_services:
        existing = set(names)
        for svc in v1_services:
            if isinstance(svc, str) and svc not in existing:
                names.append(svc)
                existing.add(svc)

    return names


def _filter_empty_service_entries(services: list[str | dict]) -> list[str | dict]:
    """Filter out dict service entries with None or empty values.

    The form system can leave behind entries like ``{"persistent-storage": None}``
    when a service was selected but not configured. These should not be treated
    as active services.

    String entries are always kept. Dict entries are kept only when their
    value is not None and not an empty dict/list.
    """
    result: list[str | dict] = []
    for entry in services:
        if isinstance(entry, str):
            result.append(entry)
        elif isinstance(entry, dict):
            for value in entry.values():
                if value is not None and value != {} and value != []:
                    result.append(entry)
                break  # single-key dicts
    return result


def save_project_file(file_path: str, project_data: dict[str, Any]) -> None:
    """
    Save project data to a YAML file.

    This function writes the project data back to disk, preserving
    the YAML structure as much as possible.

    Args:
        file_path: Full path to the project YAML file
        project_data: The project data dictionary to save
    """
    logger.info(f"Saving project file: {file_path}")
    save_yaml_to_path(file_path, project_data)
    logger.debug(f"Successfully saved project file: {file_path}")


def create_project_file_handler() -> ProjectFileHandler:
    """
    Create and return a ProjectFileHandler instance.

    Returns:
        ProjectFileHandler instance
    """
    logger.debug("Creating ProjectFileHandler")
    return ProjectFileHandler()
