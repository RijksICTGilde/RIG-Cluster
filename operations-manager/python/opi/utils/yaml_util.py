"""
Generic YAML utility module for loading, finding, and updating YAML files using JSONPath.

This module provides centralized utilities for YAML file operations using JSONPath expressions.
"""

import logging
import os
from typing import Any

from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)


def load_yaml_from_path(file_path: str) -> dict[str, Any] | None:
    """
    Load YAML content from a file path.

    Args:
        file_path: Path to the YAML file

    Returns:
        Parsed YAML data as dictionary, or None if loading failed
    """
    try:
        if not os.path.exists(file_path):
            logger.error(f"YAML file not found: {file_path}")
            return None

        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096

        with open(file_path, encoding="utf-8") as f:
            data = yaml.load(f)

        return data

    except Exception as e:
        logger.exception(f"Error loading YAML file {file_path}: {e}")
        return None


def save_yaml_to_path(file_path: str, data: dict[str, Any]) -> bool:
    """
    Save YAML data to a file path.

    Args:
        file_path: Path where to save the YAML file
        data: Dictionary containing YAML data

    Returns:
        True if save was successful, False otherwise
    """
    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096
        yaml.default_flow_style = False

        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        logger.debug(f"Successfully saved YAML to: {file_path}")
        return True

    except Exception as e:
        logger.exception(f"Error saving YAML file {file_path}: {e}")
        return False


def find_value_by_jsonpath(data: dict[str, Any], json_path: str, default: Any = None) -> Any:
    """
    Find a value in YAML data using JSONPath expression.

    Args:
        data: YAML data as dictionary
        json_path: JSONPath expression to query the data
        default: Default value to return if path not found

    Returns:
        The value found at the JSONPath, or default if not found
    """
    try:
        if not data:
            return default

        jsonpath_expr = jsonpath_parse(json_path)
        matches = jsonpath_expr.find(data)

        return matches[0].value if matches else default

    except Exception as e:
        logger.error(f"Error querying JSONPath '{json_path}': {e}")
        return default


def load_yaml_from_string(yaml_string: str) -> dict[str, Any] | None:
    """
    Load YAML content from a string.

    Args:
        yaml_string: YAML content as string

    Returns:
        Parsed YAML data as dictionary, or None if parsing failed
    """
    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096

        from io import StringIO

        data = yaml.load(StringIO(yaml_string))

        return data

    except Exception as e:
        logger.exception(f"Error parsing YAML string: {e}")
        return None


def dump_yaml_to_string(data: dict[str, Any]) -> str:
    """
    Dump YAML data to a string.

    Args:
        data: Dictionary containing YAML data

    Returns:
        YAML content as string
    """
    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096
        yaml.default_flow_style = False

        from io import StringIO

        output = StringIO()
        yaml.dump(data, output)

        return output.getvalue()

    except Exception as e:
        logger.exception(f"Error dumping YAML to string: {e}")
        return ""


def update_value_by_jsonpath(data: dict[str, Any], json_path: str, new_value: Any) -> bool:
    """
    Update a value in YAML data using JSONPath expression.

    Args:
        data: YAML data as dictionary (will be modified in-place)
        json_path: JSONPath expression to the field to update
        new_value: New value to set

    Returns:
        True if update was successful, False otherwise
    """
    try:
        if not data:
            logger.error("Cannot update value in empty data")
            return False

        jsonpath_expr = jsonpath_parse(json_path)
        matches = jsonpath_expr.find(data)

        if not matches:
            logger.error(f"JSONPath '{json_path}' not found in data")
            return False

        # Update the first match (there should typically be only one)
        matches[0].full_path.update(data, new_value)

        logger.debug(f"Successfully updated JSONPath '{json_path}' to: {new_value}")
        return True

    except Exception as e:
        logger.exception(f"Error updating JSONPath '{json_path}': {e}")
        return False
