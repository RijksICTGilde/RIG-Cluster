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
from opi.utils.age import decrypt_password_smart_sync, get_decoded_project_private_key
from opi.utils.env_vars import validate_and_parse_env_vars

logger = logging.getLogger(__name__)


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
            raise e

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
        if "dictionary_item_added" in diff:
            for path, value in diff["dictionary_item_added"].items():
                # Parse path like "root['deployments']['web-app']"
                clean_path = self._parse_deepdiff_path(path)
                changes["added"][clean_path] = value
                logger.debug(f"Added: {clean_path}")

        if "iterable_item_added" in diff:
            for path, value in diff["iterable_item_added"].items():
                clean_path = self._parse_deepdiff_path(path)
                changes["added"][clean_path] = value
                logger.debug(f"Added (iterable): {clean_path}")

        # Handle removed items (dictionary_item_removed, iterable_item_removed)
        if "dictionary_item_removed" in diff:
            for path, value in diff["dictionary_item_removed"].items():
                clean_path = self._parse_deepdiff_path(path)
                changes["deleted"][clean_path] = value
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
        clean_path = path.replace("root", "")
        # Convert ['key'] to .key, handling nested brackets
        clean_path = re.sub(r"\['([^']+)']", r".\1", clean_path)
        # Remove leading dot
        clean_path = clean_path.lstrip(".")

        return clean_path

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
            The extracted value (decrypted if needed) or default if not found
        """
        jsonpath_expr = jsonpath_parse(path)
        matches = jsonpath_expr.find(data)

        if not matches:
            logger.debug(f"No matches found for JSONPath: {path}")
            return default

        if private_key:
            return self._decrypt_with_private_key(matches[0].value, private_key)
        else:
            return matches[0].value

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

    def extract_component_storage(self, project_data: dict[str, Any], component_name: str) -> list[dict[str, Any]]:
        """
        Extract storage configuration from a component definition by name.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find storage for

        Returns:
            List of storage configurations or empty list if no storage found
        """
        # Use JSONPath with extended parser to find the component by name and extract its storage config
        path = f"$.components[?(@.name='{component_name}')].storage"
        storage_config = self.extract_value_by_path(project_data, path, [])

        if storage_config:
            logger.info(f"Found {len(storage_config)} storage config(s) for component '{component_name}'")
            return storage_config
        else:
            logger.debug(f"No storage configuration found for component '{component_name}'")
            return []

    async def extract_component_user_env_vars(
        self, project_data: dict[str, Any], component_name: str
    ) -> dict[str, str]:
        """
        Extract user environment variables from a component definition by name.

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find user env vars for

        Returns:
            Dictionary of user environment variables or empty dict if none found
        """

        private_key = await get_decoded_project_private_key(project_data)

        # Use JSONPath with extended parser to find the component by name and extract its user-env-vars
        # TODO: user-env-vars is optional, so we need to check if this path exists before we continu
        path = f"$.components[?(@.name='{component_name}')].user-env-vars"
        user_env_vars_str = self.extract_value_by_path(project_data, path, {}, private_key)
        user_env_vars = validate_and_parse_env_vars(user_env_vars_str)

        if user_env_vars:
            logger.info(f"Found {len(user_env_vars)} user environment variable(s) for component '{component_name}'")
            # Clean up and decrypt user environment variables
            cleaned_env_vars = {}

            for key, value in user_env_vars.items():
                # Convert to string and clean up quotes
                value_str = str(value) if value is not None else ""
                # Normalize and decrypt the value
                normalized_value = self._normalize_age_content(value_str)
                value_str = decrypt_password_smart_sync(normalized_value, private_key)

                # Handle regular values with quote cleaning
                if value_str == '""' or value_str == "''":
                    # Convert quoted empty strings to actual empty strings
                    cleaned_env_vars[key] = ""
                elif len(value_str) >= 2:
                    # Remove surrounding quotes if present (but preserve internal quotes)
                    if (value_str.startswith('"') and value_str.endswith('"') and value_str.count('"') == 2) or (
                        value_str.startswith("'") and value_str.endswith("'") and value_str.count("'") == 2
                    ):
                        cleaned_env_vars[key] = value_str[1:-1]
                    else:
                        cleaned_env_vars[key] = value_str
                else:
                    cleaned_env_vars[key] = value_str

            return cleaned_env_vars
        else:
            logger.debug(f"No user environment variables found for component '{component_name}'")
            return {}

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
            True if publish-on-web service is in the component's uses-services array, False otherwise
        """
        # Check uses-services array for publish-on-web service
        uses_services_path = f"$.components[?(@.name='{component_name}')].uses-services"
        uses_services = self.extract_value_by_path(project_data, uses_services_path, [])
        component_services = ServiceAdapter.parse_services_from_strings(uses_services or [])
        has_publish_service = ServiceType.PUBLISH_ON_WEB in component_services

        logger.debug(f"Component '{component_name}' has publish-on-web service: {has_publish_service}")
        return has_publish_service


def create_project_file_handler() -> ProjectFileHandler:
    """
    Create and return a ProjectFileHandler instance.

    Returns:
        ProjectFileHandler instance
    """
    logger.debug("Creating ProjectFileHandler")
    return ProjectFileHandler()
