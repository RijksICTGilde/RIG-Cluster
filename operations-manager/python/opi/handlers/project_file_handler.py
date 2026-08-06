"""
Project File Handler for managing project YAML files and change detection.

This module provides functionality to read, parse, and analyze changes in project files
including git-based diff generation and structured change extraction.
"""

import base64
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deepdiff import DeepDiff
from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml import YAML

from opi.services import ServiceAdapter, ServiceType
from opi.services.project import Project
from opi.services.resource_analyzer import _k8s_memory_to_mb
from opi.services.schema_migration import migrate_to_latest
from opi.utils.age import decrypt_age_block_to_bytes, decrypt_password_smart_sync, get_decoded_project_private_key
from opi.utils.env_vars import validate_and_parse_env_vars
from opi.utils.yaml_util import load_yaml_from_string, save_yaml_to_path

if TYPE_CHECKING:
    from opi.connectors.git import GitConnector

logger = logging.getLogger(__name__)


# Container "waiting" reasons that mean the image cannot be pulled/made available.
# Canonical location: oom_watcher imports this for live pod-health detection and
# the re-enable logic below matches against it, so the two never drift.
IMAGE_PULL_REASONS = {
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "ErrImageNeverPull",
    "ImageInspectError",
    "RegistryUnavailable",
}


def is_image_pull_disable_reason(reason: str) -> bool:
    """Whether a ``disabled-reason`` string was produced by an image-pull failure.

    The reason is stored as ``f"{waiting_reason}: {message}"`` (see oom_watcher),
    so match on the leading token.
    """
    return any(reason.startswith(r) for r in IMAGE_PULL_REASONS)


# Default resource values for deployment containers
DEFAULT_RESOURCES: dict[str, str] = {
    "requests_memory": "128Mi",
    "requests_cpu": "50m",
    "limits_memory": "512Mi",
    "limits_cpu": "500m",
}


@dataclass
class ResourceFloor:
    """OOM-watcher memory floor with the timestamp of the entry that set it."""

    floor_mb: float
    set_at: str | None


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


# URL-path charset voor tenant-bestuurde component match/rewrite waarden;
# voorkomt nginx-snippet-injection in ingress.yaml.jinja.
_SAFE_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9/_.~-]*$")


def _sanitize_path_value(value: Any, field: str) -> str:
    """
    Validate a tenant-supplied URL path used for ingress routing/rewriting.

    Raises:
        ValueError: If the value is not a string or contains characters
            outside the safe URL-path charset (defense against nginx
            configuration-snippet injection).
    """
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004
            f"Component path '{field}' must be a string, got {type(value).__name__}"
        )
    if not value:
        raise ValueError(f"Component path '{field}' must not be empty")
    if not _SAFE_PATH_PATTERN.match(value):
        raise ValueError(
            f"Component path '{field}' contains illegal characters: {value!r}. "
            f"Only '/', letters, digits and '-._~' are allowed (no whitespace, "
            f"quotes or newlines)."
        )
    return value


def _normalize_path_config(raw: Any, default_match: str = "/") -> dict[str, str | None]:
    """
    Build a validated {"match": ..., "rewrite": ...} entry from a raw path item.

    Both `match` and `rewrite` are sanitized against the safe URL-path charset.
    """
    if isinstance(raw, dict):
        match_value = raw.get("match", default_match)
        rewrite_value = raw.get("rewrite")
    else:
        match_value = raw
        rewrite_value = None

    sanitized_match = _sanitize_path_value(match_value, "match")
    sanitized_rewrite = None if rewrite_value is None else _sanitize_path_value(rewrite_value, "rewrite")
    return {"match": sanitized_match, "rewrite": sanitized_rewrite}


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
    and migrates any legacy flat ``cpu``/``memory`` keys into the nested
    structure before removing them to prevent data loss.
    """
    if "resources" not in target:
        target["resources"] = {}
    res = target["resources"]
    if "requests" not in res:
        res["requests"] = {}
    if "limits" not in res:
        res["limits"] = {}

    # Migrate legacy flat keys into the nested structure before they could
    # be removed.  This prevents silently losing CPU or memory values that
    # only existed in the old flat format.
    _migrate_flat_key_before_apply(res, "cpu")
    _migrate_flat_key_before_apply(res, "memory")

    if "requests_memory" in resources:
        res["requests"]["memory"] = resources["requests_memory"]
    if "requests_cpu" in resources:
        res["requests"]["cpu"] = resources["requests_cpu"]
    if "limits_memory" in resources:
        res["limits"]["memory"] = resources["limits_memory"]
    if "limits_cpu" in resources:
        res["limits"]["cpu"] = resources["limits_cpu"]


def _prune_resource_history(history: list[dict[str, Any]], max_entries: int) -> list[dict[str, Any]]:
    """Prune a newest-first history to ``max_entries``, always keeping the newest
    ``oom-watcher`` entry.

    The most recent oom-watcher entry is the OOM floor
    (``get_resource_history_floor``); a burst of auto-tune entries must not push it
    out of the cap and silently drop the floor. When it falls outside the cap it
    replaces the oldest kept entry, so the total still respects ``max_entries``.
    """
    if len(history) <= max_entries:
        return history
    kept = history[:max_entries]
    if any(entry.get("source") == "oom-watcher" for entry in kept):
        return kept
    for entry in history:  # newest-first: first match is the newest oom-watcher
        if entry.get("source") == "oom-watcher":
            return [*kept[:-1], entry]
    return kept


def _compact_resource_history_list(history: list[dict[str, Any]], max_entries: int) -> list[dict[str, Any]]:
    """Collapse consecutive identical auto-tune entries, then prune.

    A run of adjacent ``auto-tune`` entries with the same ``limits`` and ``requests``
    is folded to its newest member (the older duplicates carry no information). The
    newest oom-watcher entry is always preserved (``_prune_resource_history``). An
    oom-watcher entry between two auto-tune entries breaks the run.
    """
    compacted: list[dict[str, Any]] = []
    for entry in history:  # newest-first
        if (
            entry.get("source") == "auto-tune"
            and compacted
            and compacted[-1].get("source") == "auto-tune"
            and compacted[-1].get("limits") == entry.get("limits")
            and compacted[-1].get("requests") == entry.get("requests")
        ):
            continue  # identical older duplicate; the newer one is already kept
        compacted.append(entry)
    return _prune_resource_history(compacted, max_entries)


def _migrate_flat_key_before_apply(res: dict[str, Any], key: str) -> None:
    """Migrate a legacy flat resource key into requests/limits if present.

    Handles three legacy formats:
    - ``cpu: {request: "50m", limit: "1"}`` — dict with request/limit
    - ``cpu: "1"`` or ``memory: "256Mi"`` — plain string treated as limit
    - ``memory: {request: "128Mi", limit: "256Mi"}`` — dict with request/limit

    Only fills in nested values that are not already set, then removes
    the flat key so the dual-format entry is cleaned up.
    """
    value = res.get(key)
    if value is None:
        return

    if isinstance(value, dict):
        if "request" in value and key not in res.get("requests", {}):
            res.setdefault("requests", {})[key] = str(value["request"])
        if "limit" in value and key not in res.get("limits", {}):
            res.setdefault("limits", {})[key] = str(value["limit"])
    elif isinstance(value, str | int | float):
        if key not in res.get("limits", {}):
            res.setdefault("limits", {})[key] = str(value)
        if key == "memory" and key not in res.get("requests", {}):
            res.setdefault("requests", {})[key] = str(value)

    del res[key]


class ProjectFileHandler:
    """Handler for project file operations including reading, parsing, and change detection."""

    def __init__(self) -> None:
        """Initialize the ProjectFileHandler."""
        logger.debug("Initializing ProjectFileHandler")
        self._project_data: dict[str, str | list | dict[str, str]] | None = None
        self._was_migrated: bool = False
        # Deployments whose component extraction was already logged; the helper
        # is called many times per deployment in one run and would spam DEBUG.
        self._component_extraction_logged: set[str] = set()

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

    # read_project_file() was removed together with its last caller. It read a
    # project file straight off the filesystem, which for the shared warm working
    # copy races ProjectStore.reconcile()'s `reset --hard` + `clean -fd`. Change
    # detection now uses read_committed_project_file(), which reads the committed
    # object and cannot tear. Nothing should read a project file from a path again.

    @property
    def was_migrated(self) -> bool:
        """Whether the last committed-file read required schema migration."""
        return self._was_migrated

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

    async def read_committed_project_file(
        self, git_connector: GitConnector, relative_file_path: str
    ) -> dict[str, str | list | dict[str, str]]:
        """Read and parse the COMMITTED project file, straight from git objects.

        Deliberately not a working-tree read. The warm working copy is shared, and
        ``ProjectStore.reconcile()`` does ``reset --hard`` plus ``clean -fd`` on it
        under the store's lock -- which this code path does not hold. Reading the
        file from disk therefore races a routine reconcile (30s TTL, triggered by
        ordinary page loads) and can observe a half-rewritten or missing file.
        ``git show`` reads immutable objects, so it cannot tear.

        Sets ``was_migrated`` exactly like read_project_file does, so the caller's
        auto-migration commit keeps working.
        """
        if not self._project_data:
            content = await git_connector.show_file_at("HEAD", relative_file_path)
            if content is None:
                raise ValueError(f"Project file not found at HEAD: {relative_file_path}")
            raw_data = load_yaml_from_string(content)
            if raw_data is None:
                raise ValueError(f"Project file is empty or invalid YAML: {relative_file_path}")
            migrated_data, self._was_migrated = migrate_to_latest(raw_data)
            self._project_data = migrated_data
        return self._project_data

    async def analyze_project_changes(self, git_connector: GitConnector, relative_file_path: str) -> dict[str, Any]:
        """
        Analyze changes between current and previous versions of a project file.

        Args:
            git_connector: GitConnector to access the repository
            relative_file_path: Relative path to the YAML file within the repository

        Returns:
            Dictionary containing:
            - current_yaml: Current YAML content
            - previous_yaml: Previous YAML content (or None)
            - diff: DeepDiff result
            - changes: Structured changes with added/changed/deleted items
        """
        # Read current YAML content from the committed object, not the shared worktree
        current_yaml = await self.read_committed_project_file(git_connector, relative_file_path)

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

    def extract_component_inbound_ports(self, project_data: dict[str, Any], component_name: str) -> list[int]:
        """Return all inbound ports of a component in declared order, or [] if none."""
        for component in project_data.get("components", []) or []:
            if isinstance(component, dict) and component.get("name") == component_name:
                inbound = (component.get("ports") or {}).get("inbound") or []
                return [p for p in inbound if isinstance(p, int)]
        return []

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

        Canonical format (v2.2+): list of dicts with ``match`` and optional ``rewrite``.
        Backward compat: plain string path (pre-migration files).

        Args:
            project_data: The parsed project data
            component_name: Name of the component to find paths for

        Returns:
            List of path configs: [{"match": "/api", "rewrite": None}, ...]
        """
        json_path = f"$.components[?(@.name='{component_name}')].path"
        path_config = self.extract_value_by_path(project_data, json_path, "/")

        # Normaliseer naar list-format; sanitizer valideert per item.
        if isinstance(path_config, str):
            logger.debug(f"Found single path '{path_config}' for component '{component_name}'")
            return [_normalize_path_config(path_config)]
        if isinstance(path_config, list):
            result = [_normalize_path_config(p) for p in path_config]
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

        Checks deployment-level path first (deployments[].components[].path),
        then falls back to component-level path (components[].path).

        Args:
            project_data: The parsed project data
            deployment_data: The specific deployment dict
            component_reference: Name of the component to find paths for

        Returns:
            List of path configs: [{"match": "/api", "rewrite": None}, ...]
        """
        # Check deployment-level path first
        components = deployment_data.get("components", [])
        for comp in components:
            if comp.get("reference") == component_reference:
                # Schema: singular `path` (str | list); normaliseer via sanitizer.
                path_config = comp.get("path")
                if path_config is not None:
                    if isinstance(path_config, str):
                        result = [_normalize_path_config(path_config)]
                    elif isinstance(path_config, list):
                        result = [_normalize_path_config(p) for p in path_config]
                    else:
                        result = [{"match": "/", "rewrite": None}]
                    if result and result != [{"match": "/", "rewrite": None}]:
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
        """Find a component dict by name in project data.

        Delegates to the shared Project (RC-5 consolidation) so this
        reference lookup uses the one reference-aware access layer instead of a
        hand-rolled scan.
        """
        return Project(project_data).find("components", name=component_name)

    def _decrypt_and_clean_env_vars(self, env_vars: dict[str, Any], private_key: str | None) -> dict[str, str]:
        """Decrypt individual env var values.

        Each value is run through the smart decrypt (handling plain, age-encrypted,
        or empty values). Quote handling is intentionally NOT done here: the values
        have already passed through ``validate_and_parse_env_vars``, which is the
        single source of truth for quote semantics (it honours YAML quoting and
        strips ``KEY=VALUE`` quotes exactly once). Stripping again here would remove
        quotes that the user deliberately made part of the value -- e.g. an app that
        needs ``ALLOWED_HOSTNAMES`` to literally contain ``"host"`` so it can be
        wrapped into a JSON array downstream.
        """
        cleaned: dict[str, str] = {}
        for key, value in env_vars.items():
            value_str = str(value) if value is not None else ""
            normalized_value = self._normalize_age_content(value_str)
            cleaned[key] = decrypt_password_smart_sync(normalized_value, private_key)
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

    async def resolve_attachments_for_component(
        self, project_data: dict[str, Any], component_name: str, deployment_name: str | None = None
    ) -> list[dict[str, Any]]:
        """Resolve a component's attachment coupling to decrypted byte content.

        When *deployment_name* is given, the deployment component's coupling is merged
        on top of the base component's: union by ``reference`` with the deployment
        winning on a collision (mirrors the user-env-vars override). After merging, the
        delivery targets must be unique (no two references on the same ``path`` or the
        same ``env-name``).

        Returns a list of dicts with keys: reference, provide-as, path, env-name, filename,
        content_bytes. Uses the dedicated byte-decrypt (age block -> base64 -> bytes), NOT the
        string-based env-var decrypt. Raises ValueError on a dangling reference, a delivery
        conflict, or when binary content is requested as an env-var value.
        """
        component = next(
            (c for c in project_data.get("components", []) if isinstance(c, dict) and c.get("name") == component_name),
            None,
        )
        base_uses = extract_component_attachment_uses(component) if component else []
        if deployment_name:
            dep_uses = extract_deployment_component_attachment_uses(project_data, deployment_name, component_name)
            uses = _merge_attachment_uses(base_uses, dep_uses)
        else:
            uses = base_uses
        if not uses:
            return []

        _assert_unique_attachment_targets(uses, component_name, deployment_name)

        catalog = extract_attachment_catalog(project_data)
        private_key = await get_decoded_project_private_key(project_data)
        resolved: list[dict[str, Any]] = []
        for use in uses:
            reference = use.get("reference")
            if not reference:
                continue
            entry = catalog.get(reference)
            if entry is None:
                raise ValueError(f"Onbekende bijlage-referentie '{reference}'")
            content_bytes = await decrypt_age_block_to_bytes(entry["content"], private_key)
            provide_as = use.get("provide-as")
            if provide_as == "env-var":
                try:
                    content_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"Bijlage '{reference}' is binair en kan niet als env-var "
                        f"'{use.get('env-name')}' worden geleverd; gebruik provide-as: file"
                    ) from exc
            resolved.append(
                {
                    "reference": reference,
                    "provide-as": provide_as,
                    "path": use.get("path"),
                    "env-name": use.get("env-name"),
                    "filename": entry.get("filename"),
                    "content_bytes": content_bytes,
                }
            )
        return resolved

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

    def _resolve_publish_on_web_config(
        self, project_data: dict[str, Any], component_name: str, deployment_name: str | None = None
    ) -> dict[str, Any]:
        """Resolve the winning publish-on-web config block for a component.

        Cascade: per-deployment override > component > root. Returns the whole config
        dict (``{tls, attachment}``) from the first level that defines a valid ``tls``,
        so the mode and its certificate attachment always come from the same level.
        Returns ``{}`` when nothing is configured. See features/publish-on-web-tls-modes.md.
        """
        valid = ("standard", "passthrough", "provided")

        # 1. Per-deployment-component override (under services.publish-on-web.config)
        if deployment_name:
            for dep in project_data.get("deployments", []):
                if dep.get("name") != deployment_name:
                    continue
                for comp in dep.get("components", []):
                    if comp.get("reference") == component_name:
                        cfg = ((comp.get("services") or {}).get("publish-on-web") or {}).get("config") or {}
                        if cfg.get("tls") in valid:
                            return cfg
                break

        # 2. Component-level. Format-agnostic: the legacy-only read (isinstance
        # entry.get("publish-on-web")) missed a {reference: publish-on-web, config} record.
        from opi.services.services import service_entry_config, service_entry_name

        component = self._find_component(project_data, component_name)
        if component:
            for entry in component.get("services", []):
                if service_entry_name(entry) == ServiceType.PUBLISH_ON_WEB.value:
                    cfg = service_entry_config(entry) or {}
                    if isinstance(cfg, dict) and cfg.get("tls") in valid:
                        return cfg

        # 3. Root (project services) default
        for entry in project_data.get("services", []):
            if service_entry_name(entry) == ServiceType.PUBLISH_ON_WEB.value:
                cfg = service_entry_config(entry) or {}
                if isinstance(cfg, dict) and cfg.get("tls") in valid:
                    return cfg

        return {}

    def extract_component_publish_on_web_tls(
        self, project_data: dict[str, Any], component_name: str, deployment_name: str | None = None
    ) -> str:
        """Return the resolved publish-on-web TLS mode for a component.

        ``standard`` (default): the platform issues the certificate (cert-manager).
        ``passthrough``: the ingress passes TLS through untouched and the pod presents
        its own certificate; no platform certificate is issued.
        ``provided``: the customer's own certificate, terminated on the ingress.

        Resolution cascade: per-deployment override > component > root > ``standard``.
        """
        return (
            self._resolve_publish_on_web_config(project_data, component_name, deployment_name).get("tls") or "standard"
        )

    async def resolve_publish_on_web_certificate(
        self, project_data: dict[str, Any], component_name: str, deployment_name: str | None = None
    ) -> dict[str, str] | None:
        """For ``tls: provided``, resolve the referenced PEM attachment to a TLS Secret.

        Reads the catalog attachment named by ``config.attachment``, AGE-decrypts it,
        splits the PEM into the certificate chain and private key, and returns the
        base64-encoded ``{"tls.crt": ..., "tls.key": ...}`` for a ``kubernetes.io/tls``
        Secret. Returns ``None`` when the mode is not ``provided``. Raises ``ValueError``
        on a missing attachment reference, a dangling reference, or a malformed PEM.
        """
        cfg = self._resolve_publish_on_web_config(project_data, component_name, deployment_name)
        if cfg.get("tls") != "provided":
            return None

        reference = cfg.get("attachment")
        if not reference:
            raise ValueError(f"Component '{component_name}': tls 'provided' requires a certificate attachment")

        entry = extract_attachment_catalog(project_data).get(reference)
        if not entry:
            raise ValueError(
                f"Component '{component_name}': publish-on-web certificate references unknown attachment '{reference}'"
            )

        private_key = await get_decoded_project_private_key(project_data)
        pem_bytes = await decrypt_age_block_to_bytes(entry["content"], private_key)
        cert_pem, key_pem = _split_tls_pem(pem_bytes.decode("utf-8"), component_name)
        return {
            "tls.crt": base64.b64encode(cert_pem.encode("utf-8")).decode("ascii"),
            "tls.key": base64.b64encode(key_pem.encode("utf-8")).decode("ascii"),
        }

    def extract_component_command(self, project_data: dict[str, Any], component_name: str) -> list[str] | None:
        """Extract the optional component-level ``command`` override.

        Returns the user-supplied list[str] when present and well-formed, or
        ``None`` when no override exists. Non-list / empty-list / non-string
        items are silently dropped so a malformed YAML cannot crash manifest
        rendering (the JSON schema catches wrong shapes earlier; this is
        defence in depth).

        Hidden YAML-only feature — not exposed in the wizard / detail-edit UI.
        """
        component = self._find_component(project_data, component_name)
        if not component:
            return None

        raw = component.get("command")
        if not isinstance(raw, list) or not raw:
            return None
        if not all(isinstance(item, str) for item in raw):
            return None
        return list(raw)

    def extract_deployment_component_command(
        self, project_data: dict[str, Any], deployment_name: str, component_reference: str
    ) -> list[str] | None:
        """Extract the optional per-deployment ``command`` override.

        Walks ``deployments[?(name==deployment_name)].components[?(reference==component_reference)].command``.
        Returns ``None`` when no deployment-level override is present, so the
        caller can fall back to the component-level command (or the image
        default). Same defensive type checks as the component-level extractor.
        """
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") != deployment_name:
                continue
            for comp in deployment.get("components", []):
                if comp.get("reference") != component_reference:
                    continue
                raw = comp.get("command")
                if not isinstance(raw, list) or not raw:
                    return None
                if not all(isinstance(item, str) for item in raw):
                    return None
                return list(raw)
        return None

    def extract_component_security(self, project_data: dict[str, Any], component_name: str) -> dict[str, int] | None:
        """Extract the optional ``security`` block from a component definition.

        Returns a dict with any of ``run-as-user`` / ``run-as-group`` / ``fs-group``
        that were set by the user, or ``None`` if no ``security`` block exists.
        Only integer values are returned; bogus types are silently dropped so a
        malformed YAML cannot crash manifest rendering (the JSON schema catches
        wrong types earlier; this is defence in depth).

        Hidden YAML-only feature — not exposed in the wizard / detail-edit UI.
        """
        component = self._find_component(project_data, component_name)
        if not component:
            return None

        raw = component.get("security")
        if not isinstance(raw, dict):
            return None

        result: dict[str, int] = {}
        for key in ("run-as-user", "run-as-group", "fs-group"):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] = value

        return result or None

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
                comp["resources"]["history"] = _prune_resource_history(history, max_entries)
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
                comp["resources"]["history"] = _prune_resource_history(history, max_entries)
                return True
        return False

    def compact_resource_history(self, project_data: dict[str, Any], max_entries: int = 5) -> bool:
        """Compact every component's resource history in place (task 7).

        Folds runs of identical consecutive auto-tune entries and prunes to
        ``max_entries`` while always keeping the newest oom-watcher entry, at both the
        component-definition and deployment-component levels. Cleans windows that
        already filled with auto-tune noise; the service calls this just before it
        commits an already-needed change, so it costs no extra commit and never
        rewrites a project that has nothing else to change.

        Returns True if any history list was modified.
        """
        changed = False

        def compact(resources: Any) -> None:
            nonlocal changed
            if not isinstance(resources, dict):
                return
            history = resources.get("history")
            if not history:
                return
            new_history = _compact_resource_history_list(history, max_entries)
            if new_history != history:
                resources["history"] = new_history
                changed = True

        for comp in project_data.get("components", []):
            compact(comp.get("resources"))
        for dep in project_data.get("deployments", []):
            for comp in dep.get("components", []):
                compact(comp.get("resources"))
        return changed

    def get_resource_history_floor(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
        component_reference: str,
    ) -> ResourceFloor | None:
        """
        Get the OOM watcher memory floor from resource history.

        Checks both deployment-component and component-definition history.
        Only entries belonging to this deployment count: the component-definition
        history is filtered on the entry's "deployment" field, so an OOM in one
        (PR) deployment does not pin the resources of every other deployment.
        Returns the highest OOM watcher limit found in the most recent
        oom-watcher entry at either level.

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_reference: Reference name of the component

        Returns:
            ResourceFloor with value in MB and entry timestamp, or None if no
            OOM watcher entries exist for this deployment
        """
        floor: ResourceFloor | None = None

        def consider(entry: dict[str, Any]) -> ResourceFloor | None:
            value = entry.get("limits", {}).get("memory")
            if not value:
                return floor
            mb = _k8s_memory_to_mb(value)
            if floor is None or mb > floor.floor_mb:
                return ResourceFloor(floor_mb=mb, set_at=entry.get("timestamp"))
            return floor

        # Check deployment-component history
        for dep in project_data.get("deployments", []):
            if dep.get("name") != deployment_name:
                continue
            for comp in dep.get("components", []):
                if comp.get("reference") != component_reference:
                    continue
                for entry in comp.get("resources", {}).get("history", []):
                    if entry.get("source") == "oom-watcher":
                        floor = consider(entry)
                        break  # only check most recent oom-watcher entry

        # Check component-definition history (scoped to this deployment)
        for comp in project_data.get("components", []):
            if comp.get("name") != component_reference:
                continue
            for entry in comp.get("resources", {}).get("history", []):
                if entry.get("source") == "oom-watcher" and entry.get("deployment") == deployment_name:
                    floor = consider(entry)
                    break

        return floor

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

    def extract_auto_tune_enabled(
        self, project_data: dict[str, Any], deployment_name: str, component_reference: str
    ) -> bool:
        """
        Whether auto-tuning is enabled for a component within a deployment.

        Auto-tuning is opt-out: enabled unless ``auto-tune-resources`` is set to
        false. A deployment-level override wins over the component definition,
        which in turn wins over the default (True).

        Args:
            project_data: The parsed project data
            deployment_name: Name of the deployment
            component_reference: Reference name of the component

        Returns:
            True if auto-tuning should run for this component
        """
        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") != deployment_name:
                continue
            for comp in deployment.get("components", []):
                if comp.get("reference") != component_reference:
                    continue
                if "auto-tune-resources" in comp:
                    return bool(comp.get("auto-tune-resources"))

        # Fall back to the component definition, default enabled
        for comp in project_data.get("components", []):
            if comp.get("name") == component_reference and "auto-tune-resources" in comp:
                return bool(comp.get("auto-tune-resources"))
        return True

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
                    # Record the offending image for image-pull disables so the
                    # disable can be auto-cleared once the image changes, no matter
                    # which edit path (modal, git, image API) makes the change.
                    if is_image_pull_disable_reason(reason) and comp.get("image"):
                        comp["disabled-image"] = comp["image"]
                    else:
                        comp.pop("disabled-image", None)
                else:
                    comp.pop("disabled-reason", None)
                    comp.pop("disabled-image", None)
                logger.info(
                    f"Set component '{component_reference}' in deployment '{deployment_name}' "
                    f"disabled={disabled}" + (f" reason='{reason}'" if disabled else "")
                )
                return True
        logger.warning(
            f"Component '{component_reference}' not found in deployment '{deployment_name}' for disabled state update"
        )
        return False

    def reenable_components_with_changed_image(
        self,
        project_data: dict[str, Any],
        deployment_names: list[str] | None = None,
        force_reenable: bool = False,
    ) -> list[tuple[str, str]]:
        """Clear stale image-pull auto-disables so a fixed image can deploy again.

        When a component is auto-disabled for an image-pull failure, the offending
        image is recorded as ``disabled-image``. A disable is cleared here when:

        - the image reference changed since the disable (any edit path) -- the new
          image deserves a chance; or
        - ``force_reenable`` is set -- an explicit user (re)process asks OPI to try
          again, even on an unchanged pinned tag (e.g. after a transient registry
          5xx that left the same tag broken and then healthy). The automated
          post-disable refresh passes ``force_reenable=False``, so a still-broken
          component cannot flap on the automated retry loop.

        Modifies project_data in place.

        Args:
            project_data: The parsed project data (modified in place)
            deployment_names: Optional set of deployment names to limit to; None = all
            force_reenable: On an explicit user (re)process, clear the disable of any
                image-pull-disabled component regardless of whether the image
                reference changed. Never set on the automated post-disable refresh.

        Returns:
            List of (deployment_name, component_reference) that were re-enabled.
        """
        reenabled: list[tuple[str, str]] = []
        for deployment in project_data.get("deployments", []):
            dep_name = deployment.get("name", "")
            if deployment_names is not None and dep_name not in deployment_names:
                continue
            for comp in deployment.get("components", []):
                if not comp.get("disabled"):
                    continue
                disabled_image = comp.get("disabled-image")
                current_image = comp.get("image")
                if not (disabled_image and current_image):
                    continue  # only image-pull disables carry disabled-image
                changed = current_image != disabled_image
                if not (changed or force_reenable):
                    continue
                comp["disabled"] = False
                comp.pop("disabled-reason", None)
                comp.pop("disabled-image", None)
                reference = comp.get("reference", "")
                reenabled.append((dep_name, reference))
                cause = (
                    f"image changed since auto-disable ({disabled_image} -> {current_image})"
                    if changed
                    else f"user (re)process retry on unchanged tag ({current_image})"
                )
                logger.info(f"Re-enabling component '{reference}' in deployment '{dep_name}': {cause}")
        return reenabled

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
        from opi.services.services import service_entry_name

        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            if deployment.get("name") == deployment_name:
                services = deployment.get("services", [])

                # Services is a list of {reference: ..., config: ...}
                if isinstance(services, list):
                    for item in services:
                        # Resolve identity format-agnostically: a deployment clone-state entry
                        # may be {reference}, {name} or a bare string; matching only on
                        # ``reference`` silently missed the other forms (checklist 5).
                        if isinstance(item, dict) and service_entry_name(item) == service_type:
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
        source = Project.locate(remote_sources, name=name)
        if source is not None:
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
        chart = Project.locate(helm_charts, name=name)
        if chart is not None:
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
        """Return the deployment namespace pinned to the project name.

        Same invariant as ``project_manager.enforce_namespace_pin`` but
        local and non-mutating. Used by callers that don't go through
        ``ProjectManager.get_deployments`` (backup/restore endpoints, tasks).
        Raises ValueError on mismatch.
        """
        project_name = project_data.get("name")
        for deployment in project_data.get("deployments", []):
            if deployment.get("name") != deployment_name:
                continue
            declared = deployment.get("namespace")
            if declared is not None and project_name is not None and declared != project_name:
                raise ValueError(
                    f"Deployment '{deployment_name}' in project '{project_name}' "
                    f"gebruikt namespace '{declared}', maar de namespace "
                    f"moet gelijk zijn aan de projectnaam '{project_name}'. "
                    f"Een afwijkende namespace is niet toegestaan omdat dit toegang "
                    f"tot de resources van een ander project zou geven."
                )
            if project_name is None:
                logger.warning(f"No project name found while resolving namespace for deployment '{deployment_name}'")
                return declared
            logger.debug(f"Pinned namespace '{project_name}' for deployment '{deployment_name}'")
            return project_name

        logger.warning(f"No deployment '{deployment_name}' found while resolving namespace")
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

        first_extraction = deployment_name not in self._component_extraction_logged
        if first_extraction:
            self._component_extraction_logged.add(deployment_name)

        if components:
            if first_extraction:
                logger.debug(f"Found {len(components)} component reference(s) in deployment '{deployment_name}'")
            return components if isinstance(components, list) else [components]

        if first_extraction:
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
        helmfile = Project.locate(helmfiles, name=name)
        if helmfile is not None:
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

    def get_deployment_backup_labels(
        self,
        project_data: dict[str, Any],
        deployment_name: str,
    ) -> list[dict[str, str]]:
        """Get backup labels for services this deployment actually uses.

        Returns the subset of backupable service labels (pvc, database, minio)
        that the deployment uses, based on its components, helm-charts, and
        helmfiles.
        """
        result: list[dict[str, str]] = []
        for bl in ServiceAdapter.get_backupable_labels():
            svc_types = ServiceAdapter.get_service_types_for_backup_label(bl["label"])
            if self.deployment_uses_service(project_data, deployment_name, svc_types):
                result.append(bl)
        return result

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

            from opi.services.services import service_entry_name

            service_list = component.get("services", [])
            for service in service_list:
                # Format-agnostic: ``next(iter(keys))`` returned "reference"/"name" for a
                # uniform record, so a storage service in record form (the PVC case for
                # backups) never matched service_types and its component was missed.
                service_name = service_entry_name(service)
                if service_name is None:
                    continue
                default_ref = f"{deployment_name}-{service_name.split('-')[0]}"
                service_ref = default_ref
                # Only a legacy single-key body ({svc: {reference: ...}}) carries an
                # explicit generation reference; the uniform record does not.
                if isinstance(service, dict) and "name" not in service and "reference" not in service:
                    body = service.get(service_name)
                    if isinstance(body, dict):
                        service_ref = body.get("reference", default_ref)

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
        Extract the invite config from project data, normalized through the invite model.

        Reads the invite service config at ``services/invite/config`` (RC-13), falling back
        to the legacy top-level ``invites:`` block for files not yet migrated -- exactly the
        both-locations read that ``get_domains_config`` does for publish-on-web. The result is
        normalized through ``InviteConfig`` so every downstream reader (invite_manager,
        invite_routes, the public templates) sees stable underscore field names (``realm_roles``,
        ``restrict_domain``, ...) regardless of whether the file stores hyphen or underscore keys.

        Args:
            project_data: The parsed project data

        Returns:
            Config dict with 'default_language' and 'active' keys, or empty dict if no
            invites are configured.
        """
        from pydantic import ValidationError

        from opi.services.catalog.invite.config_model import InviteConfig

        raw = Project(project_data).service_config("invite")
        if raw is None:
            # Legacy top-level `invites:` block: flatten settings.default_language into the
            # config shape so both locations read identically downstream.
            legacy = project_data.get("invites")
            if not isinstance(legacy, dict) or not legacy:
                logger.debug("No invites configuration found in project data")
                return {}
            raw = {"active": legacy.get("active", []) or []}
            settings = legacy.get("settings")
            if isinstance(settings, dict) and settings.get("default_language"):
                raw["default-language"] = settings["default_language"]

        if not isinstance(raw, dict):
            return {}
        try:
            config = InviteConfig.model_validate(raw)
        except ValidationError:
            logger.warning("Invite config failed model validation; returning raw config")
            return raw
        normalized = config.model_dump(exclude_none=True)
        logger.debug(f"Found invites config with {len(normalized.get('active', []))} active invite(s)")
        return normalized

    def get_invite_settings(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract project-level invite settings.

        Since RC-13 the only project-wide invite setting is ``default_language``. The former
        allow-sso / allow-local / default-expiry settings were never accepted by the schema;
        their behaviour is folded into ``get_invite_auth_methods`` (both methods allowed unless
        an invite restricts them), and the expiry feature was removed.

        Args:
            project_data: The parsed project data

        Returns:
            ``{"default_language": str}`` (default 'nl').
        """
        invites = self.extract_invites_config(project_data)
        return {"default_language": invites.get("default_language", "nl")}

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
        invite = Project.locate(active_invites, key=key)
        if invite is not None:
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
        # Check invite-specific auth_methods override
        invite_auth_methods = invite.get("auth_methods")

        if invite_auth_methods:
            # Invite specifies exact methods
            return {
                "sso": "sso" in invite_auth_methods,
                "local": "local" in invite_auth_methods,
            }

        # No per-invite restriction: fall back to "both allowed" (the project-level default;
        # the realm and the invite-level auth-methods are the actual restrictions).
        return {"sso": True, "local": True}

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
    from opi.services.services import service_entry_config, service_entry_name

    storage_configs: list[dict[str, Any]] = []
    for entry in component.get("services", []):
        # Format-agnostic (bare string / legacy name-as-key / new {reference, config}).
        name = service_entry_name(entry)
        if name not in _STORAGE_SERVICE_TO_TYPE:
            continue
        storage_type = _STORAGE_SERVICE_TO_TYPE[name]
        config_items = service_entry_config(entry)
        if not isinstance(config_items, list):
            continue
        for item in config_items:
            if isinstance(item, dict):
                config = dict(item)
                config["type"] = storage_type
                storage_configs.append(config)

    return storage_configs


_PEM_CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL)
_PEM_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----.*?-----END (?:RSA |EC )?PRIVATE KEY-----", re.DOTALL
)


def _split_tls_pem(pem_text: str, component_name: str) -> tuple[str, str]:
    """Split a PEM bundle into (certificate-chain, private-key) for a kubernetes.io/tls Secret.

    Requires at least one CERTIFICATE block and exactly one PRIVATE KEY block.
    """
    certs = _PEM_CERT_RE.findall(pem_text)
    keys = _PEM_KEY_RE.findall(pem_text)
    if not certs:
        raise ValueError(f"Component '{component_name}': certificate attachment has no CERTIFICATE block")
    if len(keys) != 1:
        raise ValueError(
            f"Component '{component_name}': certificate attachment must contain exactly one PRIVATE KEY block "
            f"(found {len(keys)})"
        )
    cert_chain = "\n".join(block.strip() for block in certs) + "\n"
    key = keys[0].strip() + "\n"
    return cert_chain, key


def extract_attachment_catalog(project_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract the project-level attachments catalog (id -> {id, filename, content}).

    Stored as a project-level service entry::

        services:
        - attachments:
            data:
            - id: mtlskeystore
              filename: keystore.p12
              content: <age block>
    """
    catalog: dict[str, dict[str, Any]] = {}
    for entry in project_data.get("services", []):
        if not isinstance(entry, dict):
            continue
        service_data = entry.get("attachments")
        if not isinstance(service_data, dict):
            continue
        for item in service_data.get("data", []) or []:
            if isinstance(item, dict) and item.get("id"):
                catalog[item["id"]] = item
    return catalog


def find_attachment_data_list(services: Any) -> list[Any] | None:
    """Return the existing attachments ``data`` list within a services list, or None.

    Single source of truth for where the catalog lives, for callers that mutate it in place
    (strip/restore content, remove an entry). Read-only callers use extract_attachment_catalog.
    """
    if not isinstance(services, list):
        return None
    for entry in services:
        if isinstance(entry, dict) and isinstance(entry.get("attachments"), dict):
            data = entry["attachments"].get("data")
            if isinstance(data, list):
                return data
    return None


def extract_component_attachment_uses(component: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a component's attachment ``config`` coupling entries from its services list.

    Reads the ``config`` key (``use`` is the pre-rename name, still accepted for any
    not-yet-migrated data).
    """
    from opi.services.services import service_entry_config, service_entry_name

    uses: list[dict[str, Any]] = []
    for entry in component.get("services", []):
        # Format-agnostic (legacy {attachments: {config}} / new {reference: attachments, config}).
        if service_entry_name(entry) != "attachments":
            continue
        coupling = service_entry_config(entry)
        if coupling is None and isinstance(entry, dict):
            # Legacy pre-rename 'use' key (only on the legacy name-as-key form).
            body = entry.get("attachments")
            coupling = body.get("use", []) if isinstance(body, dict) else []
        uses.extend(item for item in (coupling or []) if isinstance(item, dict) and item.get("reference"))
    return uses


def extract_deployment_component_attachment_uses(
    project_data: dict[str, Any], deployment_name: str, component_reference: str
) -> list[dict[str, Any]]:
    """Extract the attachment coupling entries on a deployment component override.

    Stored under ``services.attachments.config`` on the deployment component. The
    deployment ``services`` is a map keyed by service name; ``attachments`` sits next to
    the system revision-map entries, and the ``config`` wrapper keeps the structure
    consistent with the base-component coupling (``services: - attachments: config:``).
    """
    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict) or dep.get("name") != deployment_name:
            continue
        for comp in dep.get("components", []):
            if isinstance(comp, dict) and comp.get("reference") == component_reference:
                attachments = (comp.get("services") or {}).get("attachments") or {}
                config = attachments.get("config") or []
                return [item for item in config if isinstance(item, dict) and item.get("reference")]
    return []


def _merge_attachment_uses(base: list[dict[str, Any]], override: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union two coupling lists by ``reference``, with *override* winning on a collision.

    Mirrors the user-env-vars override (``dict.update``): base entries apply unless the
    deployment override re-couples the same reference. Base order is preserved; new
    deployment-only references are appended.
    """
    merged: dict[str, dict[str, Any]] = {}
    for use in (*base, *override):
        if isinstance(use, dict) and use.get("reference"):
            merged[use["reference"]] = use
    return list(merged.values())


def _assert_unique_attachment_targets(
    uses: list[dict[str, Any]], component_name: str, deployment_name: str | None
) -> None:
    """Reject an invalid attachment coupling.

    Rejects a reference coupled more than once, a delivery target left empty
    (``file`` without ``path`` / ``env-var`` without ``env-name``), and two
    references mounting on the same path or the same env-var name.
    """
    where = f"component '{component_name}'" + (f" in deployment '{deployment_name}'" if deployment_name else "")
    seen_refs: set[str] = set()
    paths: dict[str, str] = {}
    env_names: dict[str, str] = {}
    for use in uses:
        ref = use.get("reference", "?")
        if ref in seen_refs:
            raise ValueError(f"Bijlage '{ref}' is meervoudig gekoppeld op {where}")
        seen_refs.add(ref)
        if use.get("provide-as") == "file":
            target = use.get("path")
            if not target:
                raise ValueError(f"Bijlage '{ref}' op {where} heeft geen pad ingesteld")
            if target in paths:
                raise ValueError(f"Bijlagen '{paths[target]}' en '{ref}' gebruiken hetzelfde pad '{target}' op {where}")
            paths[target] = ref
        elif use.get("provide-as") == "env-var":
            target = use.get("env-name")
            if not target:
                raise ValueError(f"Bijlage '{ref}' op {where} heeft geen env-var-naam ingesteld")
            if target in env_names:
                raise ValueError(
                    f"Bijlagen '{env_names[target]}' en '{ref}' gebruiken dezelfde env-var '{target}' op {where}"
                )
            env_names[target] = ref


def validate_attachment_references(project_data: dict[str, Any]) -> list[str]:
    """Return error strings for attachment references that point at an unknown catalog id.

    Covers every reference site that would otherwise fail later at deploy/resolve
    time: component attachment ``use`` couplings, deployment-component ``use``
    overrides, and publish-on-web ``provided`` certificates (root, component and
    deployment-component level) -- the same sites ``extract_attachment_usage``
    aggregates, so validation and the delete-guard never disagree.
    """
    catalog_ids = set(extract_attachment_catalog(project_data).keys())
    errors: list[str] = []
    for reference, where in extract_attachment_usage(project_data).items():
        if reference not in catalog_ids:
            locations = ", ".join(where) if where else "?"
            errors.append(f"Onbekende bijlage-referentie '{reference}' gebruikt door: {locations}")
    return errors


def validate_attachment_couplings(project_data: dict[str, Any]) -> list[str]:
    """Return error strings for structurally invalid attachment couplings.

    Checks each component's raw coupling list (a base component's, and each
    deployment-component override's) BEFORE the base<-deployment merge, so a
    duplicate reference on a base component is caught even though the merge
    (:func:`_merge_attachment_uses`) would collapse it. Rejects a reference
    coupled more than once, an empty delivery target, and colliding paths /
    env-var names -- the base-component ``services`` list is not covered by the
    JSON schema, so this is the authoritative gate for it.
    """
    errors: list[str] = []
    for component in project_data.get("components", []) or []:
        if not isinstance(component, dict):
            continue
        try:
            _assert_unique_attachment_targets(
                extract_component_attachment_uses(component), component.get("name", "?"), None
            )
        except ValueError as e:
            errors.append(str(e))
    for dep in project_data.get("deployments", []) or []:
        if not isinstance(dep, dict):
            continue
        dep_name = dep.get("name")
        if not dep_name:
            continue
        for ref in dep.get("components", []) or []:
            if not isinstance(ref, dict) or not ref.get("reference"):
                continue
            uses = extract_deployment_component_attachment_uses(project_data, dep_name, ref["reference"])
            try:
                _assert_unique_attachment_targets(uses, ref["reference"], dep_name)
            except ValueError as e:
                errors.append(str(e))
    return errors


def _publish_on_web_provided_ref(config: Any) -> str | None:
    """The attachment id of a publish-on-web config, only when its tls mode is 'provided'."""
    if isinstance(config, dict) and config.get("tls") == "provided":
        ref = config.get("attachment")
        return ref if isinstance(ref, str) and ref else None
    return None


def extract_attachment_usage(project_data: dict[str, Any]) -> dict[str, list[str]]:
    """Map each attachment id to the places that reference it (component names / labels).

    Covers component attachment ``use`` couplings, deployment-component ``use`` overrides,
    and publish-on-web ``provided`` certificates at root, component and deployment-component
    level. Used both as the delete-guard (``attachment_is_referenced``) and by the
    delete-confirmation modal, so the two never disagree.
    """
    from opi.services.services import service_entry_config, service_entry_name

    usage: dict[str, list[str]] = {}

    def add(ref: Any, label: str) -> None:
        if not isinstance(ref, str) or not ref:
            return
        names = usage.setdefault(ref, [])
        if label and label not in names:
            names.append(label)

    # Component level: attachment couplings + publish-on-web provided certificate.
    # Format-agnostic: the legacy-only read missed a {reference: publish-on-web, config}
    # record, so a certificate provided by a record-form entry escaped the delete-guard.
    for component in project_data.get("components", []):
        if not isinstance(component, dict):
            continue
        name = component.get("name", "")
        for use in extract_component_attachment_uses(component):
            add(use.get("reference"), name)
        for entry in component.get("services", []):
            if service_entry_name(entry) == ServiceType.PUBLISH_ON_WEB.value:
                add(_publish_on_web_provided_ref(service_entry_config(entry)), name)

    # Deployment-component overrides: attachment couplings + publish-on-web provided cert.
    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict):
            continue
        dep_name = dep.get("name", "")
        for comp in dep.get("components", []):
            if not isinstance(comp, dict):
                continue
            cref = comp.get("reference", "")
            label = f"{cref} ({dep_name})" if dep_name else cref
            for use in extract_deployment_component_attachment_uses(project_data, dep_name, cref):
                add(use.get("reference"), label)
            cfg = ((comp.get("services") or {}).get("publish-on-web") or {}).get("config")
            add(_publish_on_web_provided_ref(cfg), label)

    # Root publish-on-web default certificate.
    for entry in project_data.get("services", []):
        if service_entry_name(entry) == ServiceType.PUBLISH_ON_WEB.value:
            add(_publish_on_web_provided_ref(service_entry_config(entry)), "publicatie (project-breed)")

    return usage


def attachment_is_referenced(project_data: dict[str, Any], attachment_id: str) -> bool:
    """True if any component or publish-on-web certificate uses the attachment (delete-guard)."""
    return attachment_id in extract_attachment_usage(project_data)


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

    String entries are always kept. Dict entries are kept only when they carry a
    non-empty value (a name/reference or a non-empty config).
    """

    def keep(entry: str | dict) -> bool:
        if isinstance(entry, str):
            return True
        # Scan every value: the old ``break  # single-key dicts`` assumed exactly one
        # key, but a uniform record ({name/reference, config}) has more than one.
        return isinstance(entry, dict) and any(
            value is not None and value != {} and value != [] for value in entry.values()
        )

    return [entry for entry in services if keep(entry)]


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
