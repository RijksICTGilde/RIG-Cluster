"""
Generic YAML utility module for loading, finding, and updating YAML files using JSONPath.

This module provides centralized utilities for YAML file operations using JSONPath expressions.
"""

import logging
import os
import tempfile
from typing import Any

from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, SingleQuotedScalarString

logger = logging.getLogger(__name__)


def _fits_literal_block(data: str) -> bool:
    """Whether a string should be written as a literal block scalar.

    A carriage return disqualifies it: a block scalar writes the ``\\r`` out raw and
    YAML normalizes line breaks on read, so ``a\\r\\nb`` would come back as ``a\\nb``.
    Quoting escapes it and the value survives the round-trip.
    """
    return "\n" in data and "\r" not in data


def _represent_multiline_str(representer: Any, data: str) -> Any:
    """Emit a multi-line string as a literal block (``|``), not a quoted one-liner.

    AGE-encrypted values (realm passwords, api keys, user-env-vars, attachments)
    are wrapped in ``LiteralScalarString`` where they are produced, but that ruamel
    type does not survive a round-trip through JSON, which the modal-edit wizard
    session does. The value then reached the dumper as a plain ``str`` and came out
    as one unreadable line full of ``\\n`` escapes. Choosing the style here, in the
    one canonical writer, makes it hold for every write path instead of asking each
    of them to remember.
    """
    return representer.represent_scalar("tag:yaml.org,2002:str", data, style="|" if _fits_literal_block(data) else None)


_QUOTED_REPRESENTERS = {
    SingleQuotedScalarString: RoundTripRepresenter.represent_single_quoted_scalarstring,
    DoubleQuotedScalarString: RoundTripRepresenter.represent_double_quoted_scalarstring,
}


def _represent_quoted_scalarstring(representer: Any, data: str) -> Any:
    """Same rule for a scalar the loader read back as quoted.

    ``preserve_quotes`` keeps the quoting of values that were already quoted, which is
    what we want for single-line ones. A multi-line quoted scalar, though, is only ever
    the damage described above, already committed; re-emitting it verbatim would leave
    such a file broken forever. Rewriting it as a block repairs it on the next write.
    """
    if _fits_literal_block(data):
        return representer.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return _QUOTED_REPRESENTERS[type(data)](representer, data)


def _create_yaml_writer() -> YAML:
    """Create a consistently configured YAML writer for project files."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.ignore_aliases = lambda *_: True
    # Registration is by exact type, so LiteralScalarString and the other ruamel
    # scalar-string subclasses keep their own representers and are unaffected.
    yaml.representer.add_representer(str, _represent_multiline_str)
    for quoted_type in _QUOTED_REPRESENTERS:
        yaml.representer.add_representer(quoted_type, _represent_quoted_scalarstring)
    return yaml


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
    Save YAML data to a file path, atomically.

    The dump goes to a temporary file in the same directory and is then moved into place
    with os.replace, which is atomic on a single filesystem. A reader therefore sees either
    the old file or the complete new one, never a half-written one.

    This matters because the previous implementation opened the target with mode "w", which
    truncates first: a dump that raised partway (an unrepresentable value reaching the
    dumper) left a truncated or empty file behind, which a subsequent commit would then
    happily publish. Callers also ignored the False return, so that damage was silent.

    Args:
        file_path: Path where to save the YAML file
        data: Dictionary containing YAML data

    Returns:
        True if the save succeeded.

    Raises:
        OSError, ValueError: propagated from the dump or the move. Deliberately raised
            rather than returned as False, so a failed write can never be mistaken for a
            successful one by a caller that does not check the return value.
    """
    yaml = _create_yaml_writer()

    dir_name = os.path.dirname(file_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    # Same directory as the target, so os.replace stays within one filesystem.
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(file_path)}.", dir=dir_name or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    except BaseException:
        # Leave the existing file untouched and drop the partial temporary.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        logger.exception(f"Error saving YAML file {file_path}")
        raise

    logger.debug(f"Successfully saved YAML to: {file_path}")
    return True


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

    Raises:
        Propagates any dumper error. It previously returned "" on failure, which is a
        dangerous sentinel now that ProjectStore commits this string straight into git:
        an empty string would silently publish an empty project file.
    """
    yaml = _create_yaml_writer()

    from io import StringIO

    output = StringIO()
    yaml.dump(data, output)

    return output.getvalue()


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
