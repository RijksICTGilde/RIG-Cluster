from __future__ import annotations

import re
from typing import Any

_SEGMENT_RE = re.compile(r"^([^\[]+)(?:\[(\d+|\*)\])?$")


def _parse_segments(yaml_path: str) -> list[tuple[str, str | None]]:
    """Parse a yaml path into (key, index_or_wildcard) tuples."""
    segments: list[tuple[str, str | None]] = []
    for part in yaml_path.split("/"):
        m = _SEGMENT_RE.match(part)
        if not m:
            msg = f"Malformed path segment: {part!r}"
            raise ValueError(msg)
        segments.append((m.group(1), m.group(2)))
    return segments


def get_value(data: dict[str, Any], yaml_path: str) -> Any:
    """
    Extract value from YAML dict at the given path.

    Args:
        data: The YAML dict to traverse.
        yaml_path: Path string (e.g., "users[0]/email", "config/age-public-key").

    Returns:
        The value at the path, or None if the path doesn't exist.
        For wildcard paths [*], returns a list of all matching values.

    Never raises for missing data — returns None instead.
    """
    if yaml_path == "":
        return data

    return _get_recursive(data, yaml_path.split("/"))


def _get_recursive(current: Any, parts: list[str]) -> Any:
    """Recursively traverse data following path parts."""
    for i, part in enumerate(parts):
        m = _SEGMENT_RE.match(part)
        if not m:
            return None

        key = m.group(1)
        index_str = m.group(2)

        if not isinstance(current, dict):
            return None

        current = current.get(key)
        if current is None:
            return None

        if index_str is None:
            continue
        elif index_str == "*":
            if not isinstance(current, list):
                return None
            remaining = parts[i + 1 :]
            if not remaining:
                return list(current)
            return [_get_recursive(item, remaining) for item in current]
        else:
            index = int(index_str)
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]

    return current


def set_value(data: dict[str, Any], yaml_path: str, value: Any) -> dict[str, Any]:
    """
    Set a value in a YAML dict at the given path.

    Creates intermediate dicts and extends lists as needed.
    Does NOT support [*] wildcards — use resolve_path() first.

    Args:
        data: The YAML dict to modify (mutated in place).
        yaml_path: Concrete path (no wildcards).
        value: The value to set.

    Returns:
        The modified data dict.

    Raises:
        ValueError: If path contains [*] or is malformed.
    """
    if yaml_path == "":
        msg = "Cannot set value at empty path"
        raise ValueError(msg)

    if "[*]" in yaml_path:
        msg = "Cannot use wildcard [*] in set_value — use resolve_path() first"
        raise ValueError(msg)

    segments = _parse_segments(yaml_path)
    current = data

    for i, (key, index_str) in enumerate(segments):
        is_last = i == len(segments) - 1

        if is_last:
            if index_str is not None:
                index = int(index_str)
                _ensure_list(current, key, index)
                current[key][index] = value
            else:
                current[key] = value
        else:
            if index_str is not None:
                index = int(index_str)
                _ensure_list(current, key, index)
                if not isinstance(current[key][index], dict):
                    current[key][index] = {}
                current = current[key][index]
            else:
                if key not in current or not isinstance(current[key], dict):
                    current[key] = {}
                current = current[key]

    return data


def _ensure_list(container: dict[str, Any], key: str, index: int) -> None:
    """Ensure container[key] is a list with at least index+1 items."""
    if key not in container or not isinstance(container[key], list):
        container[key] = []
    lst = container[key]
    while len(lst) <= index:
        lst.append({})


def resolve_path(yaml_path: str, index: int | None = None) -> str:
    """
    Replace the first [*] wildcard with a concrete [index].

    resolve_path("users[*]/email", 2) -> "users[2]/email"
    resolve_path("a[*]/b[*]/c", 1) -> "a[1]/b[*]/c"  (only first)

    If index is None, returns path unchanged.
    """
    if index is None:
        return yaml_path
    return yaml_path.replace("[*]", f"[{index}]", 1)
