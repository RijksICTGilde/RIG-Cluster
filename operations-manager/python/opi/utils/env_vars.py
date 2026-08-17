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

        # YAML indicators: colon-based key.
        # The value side may legitimately contain '=' (e.g. URLs with
        # query strings, base64, env-style templates), so do NOT exclude
        # lines just because they have an '='. The presence of "KEY:<space>"
        # at the start of a line is the YAML signal.
        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*:(\s|$)", line):
            yaml_indicators += 1
            continue

        # KEY=VALUE indicators
        if "=" in line and not line.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", line):
            keyvalue_indicators += 1

    # If we have more YAML indicators or the text starts with certain patterns
    if yaml_indicators > keyvalue_indicators or text.strip().startswith(("---", "{\n", "[\n")):
        return "yaml"

    if yaml_indicators == 0 and keyvalue_indicators == 0:
        logger.warning("Could not detect env-var format (no YAML or KEY=VALUE indicators); falling back to keyvalue")

    return "keyvalue"


def _yaml_failure_summary(error: Exception) -> str:
    """Describe a YAML parse failure without echoing the text that failed.

    ``str()`` on a ruamel error quotes the offending source line, and this parser runs on
    a component's own environment variables, where a line may be a pasted secret. The
    problem plus its position says the same thing without carrying the value into a log
    line or an HTTP response.
    """
    mark = getattr(error, "problem_mark", None)
    if mark is None:
        # Not a marked parser error but one of the checks below, whose messages name keys
        # and types only.
        return str(error)
    problem = getattr(error, "problem", None) or type(error).__name__
    return f"{problem} at line {mark.line + 1}, column {mark.column + 1}"


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
                    raise TypeError(f"Environment variable key must be a string, got {type(key).__name__}: {key}")

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
                elif isinstance(value, int | float):
                    env_vars[key] = str(value)
                elif isinstance(value, str):
                    env_vars[key] = value
                else:
                    raise ValueError(
                        f"Environment variable '{key}' has unsupported value type {type(value).__name__}. "
                        "Only strings, numbers, and booleans are supported."
                    )
        else:
            raise TypeError("YAML must be a dictionary/mapping of key-value pairs")

        return env_vars

    except Exception as e:
        if "ValueError" in str(e.__class__):
            raise
        raise ValueError(f"Failed to parse YAML: {_yaml_failure_summary(e)}")


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
            # The line itself is never quoted back: this text is a component's own
            # environment and a line may be a pasted secret, while the caller both logs
            # this message and returns it to the client. The line NUMBER is enough to
            # find it.
            raise ValueError(f"Line {line_num}: Invalid format. Expected KEY=value")

        key, value = line.split("=", 1)  # Split only on first '=' to allow '=' in values
        key = key.strip()
        value = value.strip()

        # Clean up common problematic value formats
        if value == '""' or value == "''":
            # Convert quoted empty strings to actual empty strings
            value = ""
        elif len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"') and value.count('"') == 2)
            or (value.startswith("'") and value.endswith("'") and value.count("'") == 2)
        ):
            # Remove surrounding quotes if present (but preserve internal quotes)
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


def extract_variable_references(template: str) -> list[str]:
    """
    Extract all variable references from a template string.

    Supports both $VAR and ${VAR} syntax.

    Args:
        template: String potentially containing variable references

    Returns:
        List of unique variable names referenced in the template

    Examples:
        >>> extract_variable_references("$HOST:$PORT")
        ['HOST', 'PORT']
        >>> extract_variable_references("${USER}:${PASS}@${HOST}")
        ['USER', 'PASS', 'HOST']
        >>> extract_variable_references("mixed $VAR1 and ${VAR2}")
        ['VAR1', 'VAR2']
    """
    if not template:
        return []

    var_names = set()

    # Pattern 1: ${VAR_NAME} - braced syntax
    braced_pattern = r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
    var_names.update(re.findall(braced_pattern, template))

    # Pattern 2: $VAR_NAME - simple syntax (not followed by alphanumeric or underscore)
    simple_pattern = r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^A-Za-z0-9_]|$)"
    var_names.update(re.findall(simple_pattern, template))

    return sorted(var_names)


def substitute_variables(template: str, context: dict[str, str]) -> str:
    """
    Substitute variable references in a template string with values from context.

    Supports both $VAR and ${VAR} syntax.
    Escaping: $$ becomes literal $

    Args:
        template: String containing variable references
        context: Dictionary of variable name -> value mappings

    Returns:
        String with all variables substituted

    Raises:
        ValueError: If a referenced variable is not found in context

    Examples:
        >>> substitute_variables("$HOST:$PORT", {"HOST": "localhost", "PORT": "5432"})
        'localhost:5432'
        >>> substitute_variables("${USER}@${HOST}", {"USER": "admin", "HOST": "db.svc"})
        'admin@db.svc'
        >>> substitute_variables("Price: $$10", {})
        'Price: $10'
    """
    if not template:
        return template

    # First, handle escaping: $$ -> placeholder
    placeholder = "\x00ESCAPED_DOLLAR\x00"
    result = template.replace("$$", placeholder)

    # Extract all referenced variables
    referenced_vars = extract_variable_references(result)

    # Validate all referenced variables exist in context
    missing_vars = [var for var in referenced_vars if var not in context]
    if missing_vars:
        available = ", ".join(sorted(context.keys()))
        raise ValueError(
            f"Variable references not found in context: {', '.join(missing_vars)}. Available variables: {available}"
        )

    # Substitute braced variables: ${VAR}
    for var_name in referenced_vars:
        var_value = context[var_name]
        result = result.replace(f"${{{var_name}}}", var_value)

    # Substitute simple variables: $VAR
    # Use word boundary to avoid partial replacements
    for var_name in referenced_vars:
        var_value = context[var_name]
        # Replace $VAR but not if it's part of ${VAR}
        safe_value = var_value.replace("\\", "\\\\")
        result = re.sub(rf"\${var_name}(?=[^A-Za-z0-9_{{]|$)", safe_value, result)

    # Restore escaped dollars
    result = result.replace(placeholder, "$")

    return result


def detect_circular_references(aliases: dict[str, str]) -> None:
    """
    Detect circular references in alias definitions.

    Args:
        aliases: Dictionary of alias_name -> template mappings

    Raises:
        ValueError: If circular references are detected

    Examples:
        >>> detect_circular_references({"A": "$B", "B": "$A"})
        ValueError: Circular reference detected: A -> B -> A
    """
    if not aliases:
        return

    # Build dependency graph
    dependencies: dict[str, set[str]] = {}
    for alias_name, template in aliases.items():
        referenced_vars = extract_variable_references(template)
        # Only track dependencies on other aliases
        dependencies[alias_name] = {var for var in referenced_vars if var in aliases}

    # Check for cycles using depth-first search
    def has_cycle(node: str, visited: set[str], path: list[str]) -> list[str] | None:
        if node in visited:
            # Found a cycle
            if node in path:
                cycle_start = path.index(node)
                return [*path[cycle_start:], node]
            return None

        visited.add(node)
        path.append(node)

        for dep in dependencies.get(node, set()):
            cycle = has_cycle(dep, visited, path)
            if cycle:
                return cycle

        path.pop()
        return None

    visited: set[str] = set()
    for alias_name in aliases:
        if alias_name not in visited:
            cycle = has_cycle(alias_name, visited, [])
            if cycle:
                cycle_str = " -> ".join(cycle)
                raise ValueError(f"Circular reference detected: {cycle_str}")


#: Variables OPI exposes are uppercase by convention (PUBLIC_HOST, DATABASE_HOST, the
#: resolved aliases). Matching only that shape is what makes lenient substitution safe:
#: a dollar inside a password almost never looks like this. Measured on the production
#: files, the three real ``$`` occurrences outside a genuine reference are ``$wL7nQr4``,
#: ``$k`` and ``$x8cc5ls`` -- all lowercase, so none of them is even a candidate.
_UPPERCASE_REFERENCE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)(?=[^A-Za-z0-9_{]|$)")


def extract_uppercase_references(template: str) -> list[str]:
    """Variable references in the uppercase form OPI actually exposes."""
    if not template:
        return []
    return sorted({braced or simple for braced, simple in _UPPERCASE_REFERENCE.findall(template)})


def substitute_known_variables(template: str, context: dict[str, str], where: str = "") -> str:
    """Substitute the uppercase variables we know; leave anything else untouched.

    The lenient counterpart of :func:`substitute_variables`, for user-supplied values
    rather than aliases. The strict version raises on an unknown reference, which is
    right for an alias: the author wrote it to reference something, so a miss is a typo.
    A user-env-var value is often just a password that happens to contain a dollar, and
    failing a project's deploy over that would be wrong.

    Two things keep this from being sloppy. Only the uppercase form is a candidate at
    all, so ordinary password noise is not even considered. And an uppercase reference we
    cannot resolve is logged as a warning, because that one probably IS a typo -- with
    only the first three characters of the name, since the reference sits inside a value
    that may be a secret.

    ``$$`` still escapes to a literal ``$``.
    """
    if not template:
        return template

    placeholder = "\x00ESCAPED_DOLLAR\x00"
    result = template.replace("$$", placeholder)

    unresolved = []
    for var_name in extract_uppercase_references(result):
        if var_name not in context:
            unresolved.append(var_name)
            continue
        value = context[var_name]
        result = result.replace(f"${{{var_name}}}", value)
        # Word boundary so $PUBLIC_HOST does not eat the start of $PUBLIC_HOSTNAME.
        result = re.sub(rf"\${var_name}(?=[^A-Za-z0-9_{{]|$)", value.replace("\\", "\\\\"), result)

    if unresolved:
        hints = ", ".join(f"${name[:3]}..." for name in unresolved)
        logger.warning(
            f"Left {len(unresolved)} unresolved variable reference(s) in {where or 'a value'} as-is ({hints}); "
            f"available: {', '.join(sorted(context)) or 'none'}"
        )

    return result.replace(placeholder, "$")
