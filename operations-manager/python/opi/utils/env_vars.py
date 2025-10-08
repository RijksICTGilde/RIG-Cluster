"""
Environment variable utilities for generating deployment environment variables.

This module provides functionality to generate environment variables for deployments
including storage paths, service discovery, database connections, and other runtime
configuration variables.
"""

import logging
import re

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

logger = logging.getLogger(__name__)


def _detect_env_var_format(text: str) -> str:
    """
    Detect whether the input is in KEY=VALUE or YAML format.

    Args:
        text: Input text to analyze

    Returns:
        'yaml' if YAML format detected, 'keyvalue' otherwise
    """
    lines = text.strip().split("\n")

    # Look for YAML indicators
    yaml_indicators = 0
    keyvalue_indicators = 0

    for line in lines[:10]:  # Check first 10 lines
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # YAML indicators: indentation with colon, no equals sign
        if ":" in line and "=" not in line:
            # Check for YAML-style key: value
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*:\s*.+", line):
                yaml_indicators += 1

        # KEY=VALUE indicators
        if "=" in line and not line.startswith("-"):
            if re.match(r"^[A-Z_][A-Z0-9_]*=.*", line):
                keyvalue_indicators += 1

    # If we have more YAML indicators or the text starts with certain patterns
    if yaml_indicators > keyvalue_indicators or text.strip().startswith(("---", "{\n", "[\n")):
        return "yaml"

    return "keyvalue"


def _parse_yaml_env_vars(yaml_text: str) -> dict[str, str]:
    """
    Parse environment variables from YAML format.

    Args:
        yaml_text: YAML formatted text

    Returns:
        Dictionary of environment variables

    Raises:
        ValueError: If YAML is invalid or contains non-string values
    """
    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        data = yaml.load(yaml_text)

        if data is None:
            return {}

        # Handle different YAML structures
        env_vars = {}

        if isinstance(data, dict):
            for key, value in data.items():
                if not isinstance(key, str):
                    raise ValueError(f"Environment variable key must be a string, got {type(key).__name__}: {key}")

                # Validate key format
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                    raise ValueError(
                        f"Invalid key format '{key}'. Keys must start with A-Z, a-z, or _, contain only A-Z, a-z, 0-9, _"
                    )

                # Convert value to string
                if value is None:
                    env_vars[key] = ""
                elif isinstance(value, bool):
                    env_vars[key] = "true" if value else "false"
                elif isinstance(value, (int, float)):
                    env_vars[key] = str(value)
                elif isinstance(value, str):
                    env_vars[key] = value
                else:
                    raise ValueError(
                        f"Environment variable '{key}' has unsupported value type {type(value).__name__}. "
                        "Only strings, numbers, and booleans are supported."
                    )
        else:
            raise ValueError("YAML must be a dictionary/mapping of key-value pairs")

        return env_vars

    except Exception as e:
        if "ValueError" in str(e.__class__):
            raise
        raise ValueError(f"Failed to parse YAML: {e!s}")


def validate_and_parse_env_vars(env_vars_text: str | None) -> dict[str, str]:
    """
    Validate and parse environment variables from text format.
    Automatically detects and supports both KEY=VALUE and YAML formats.

    Supported formats:

    KEY=VALUE format:
        DATABASE_URL=postgresql://...
        API_KEY=secret123
        DEBUG=true

    YAML format:
        DATABASE_URL: postgresql://...
        API_KEY: secret123
        DEBUG: true
        PORT: 8080

    Args:
        env_vars_text: Environment variables in KEY=VALUE or YAML format

    Returns:
        Dictionary of parsed environment variables

    Raises:
        ValueError: If format is invalid
    """
    if not env_vars_text:
        return {}

    # Handle legacy CommentedMap (dict variant) - return as is
    if isinstance(env_vars_text, CommentedMap):
        return dict(env_vars_text)

    # Auto-detect format
    format_type = _detect_env_var_format(env_vars_text)

    if format_type == "yaml":
        return _parse_yaml_env_vars(env_vars_text)

    # Original KEY=VALUE parsing logic
    env_vars = {}
    lines = env_vars_text.strip().split("\n")

    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):  # Skip empty lines and comments
            continue

        if "=" not in line:
            raise ValueError(f"Line {line_num}: Invalid format. Expected KEY=value, got: {line}")

        key, value = line.split("=", 1)  # Split only on first '=' to allow '=' in values
        key = key.strip()
        value = value.strip()

        # Clean up common problematic value formats
        if value == '""' or value == "''":
            # Convert quoted empty strings to actual empty strings
            value = ""
        elif len(value) >= 2:
            # Remove surrounding quotes if present (but preserve internal quotes)
            if (value.startswith('"') and value.endswith('"') and value.count('"') == 2) or (
                value.startswith("'") and value.endswith("'") and value.count("'") == 2
            ):
                value = value[1:-1]

        if not key:
            raise ValueError(f"Line {line_num}: Environment variable key cannot be empty")

        # Validate key format (letters, numbers, underscore)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ValueError(
                f"Line {line_num}: Invalid key format '{key}'. Keys must start with A-Z, a-z, or _, contain only A-Z, a-z, 0-9, _"
            )

        env_vars[key] = value

    return env_vars
