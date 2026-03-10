"""Path resolution for navigating YAML data structures.

Supports a rich path syntax for get/set operations:

    Segment types
    ─────────────
    key            dict key lookup             "name"
    key[N]         list index                  "components[0]"
    key[*]         wildcard (all items)        "components[*]"  (get only)
    key{K}         dict-key filter in list     "services{keycloak}"
    key{F=V}       field-match filter in list  "config{name=data}"

    Full examples
    ─────────────
    components[*]/services{persistent-storage}/config[*]/name
    services{keycloak}/config/template
    deployments[0]/components{name=frontend}/image

Dict-key filter ``{K}``
    The list is a mix of strings and dicts.  Finds the dict whose
    top-level key matches *K* and descends into ``dict[K]``.
    On write, promotes a matching string to ``{K: {}}`` or appends
    a new entry if not found.

Field-match filter ``{F=V}``
    The list contains dicts.  Finds the first dict where ``dict[F] == V``
    and descends into that dict.
    On write, appends ``{F: V}`` if no match exists.
"""

from __future__ import annotations

import re
from typing import Any

# Matches: key, key[0], key[*], key{foo}, key{foo=bar}
_SEGMENT_RE = re.compile(
    r"^(?P<key>[^\[\{]+)"
    r"(?:"
    r"  \[(?P<idx>\d+|\*)\]"  # [N] or [*]
    r"  |"
    r"  \{(?P<filter>[^\}]+)\}"  # {K} or {F=V}
    r")?$",
    re.VERBOSE,
)


def _parse_segment(part: str) -> tuple[str, str | None, str | None]:
    """Parse one path segment into (key, index_or_wildcard, filter_expr).

    Returns exactly one of index/filter populated, or both None for plain keys.
    """
    m = _SEGMENT_RE.match(part)
    if not m:
        msg = f"Malformed path segment: {part!r}"
        raise ValueError(msg)
    return m.group("key"), m.group("idx"), m.group("filter")


# ---------------------------------------------------------------------------
# get_value
# ---------------------------------------------------------------------------


def get_value(data: dict[str, Any], yaml_path: str) -> Any:
    """Extract value from YAML dict at the given path.

    Supports wildcards ``[*]`` (returns list of all matches) and
    filter expressions ``{K}`` / ``{F=V}`` for mixed-list navigation.

    Never raises for missing data — returns None instead.
    """
    if yaml_path == "":
        return data
    return _get_recursive(data, yaml_path.split("/"))


def _get_recursive(current: Any, parts: list[str]) -> Any:
    """Recursively traverse data following path parts."""
    for i, part in enumerate(parts):
        key, idx, filt = _parse_segment(part)

        if not isinstance(current, dict):
            return None

        current = current.get(key)
        if current is None:
            return None

        remaining = parts[i + 1 :]

        # --- wildcard [*] ------------------------------------------------
        if idx == "*":
            if not isinstance(current, list):
                return None
            if not remaining:
                return list(current)
            return [_get_recursive(item, remaining) for item in current]

        # --- concrete index [N] ------------------------------------------
        if idx is not None:
            index = int(idx)
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
            continue

        # --- filter {K} or {F=V} -----------------------------------------
        if filt is not None:
            if not isinstance(current, list):
                return None
            current = _filter_get(current, filt)
            if current is None:
                return None
            continue

    return current


def _filter_get(items: list[Any], filt: str) -> Any:
    """Apply a filter expression to a list and return the matched value.

    {K}     → find dict with top-level key K, return dict[K]
    {F=V}   → find dict where dict[F] == V, return that dict
    """
    if "=" in filt:
        field, value = filt.split("=", 1)
        for item in items:
            if isinstance(item, dict) and str(item.get(field)) == value:
                return item
        return None

    # {K} — dict-key filter in mixed list
    for item in items:
        if isinstance(item, dict) and filt in item:
            return item[filt]
    return None


# ---------------------------------------------------------------------------
# set_value
# ---------------------------------------------------------------------------


def set_value(data: dict[str, Any], yaml_path: str, value: Any) -> dict[str, Any]:
    """Set a value in a YAML dict at the given path.

    Creates intermediate dicts, extends lists, and promotes/creates
    entries for filter expressions as needed.

    Does NOT support ``[*]`` wildcards — use ``resolve_path()`` first.

    Returns the modified data dict (mutated in place).
    """
    if yaml_path == "":
        msg = "Cannot set value at empty path"
        raise ValueError(msg)
    if "[*]" in yaml_path:
        msg = "Cannot use wildcard [*] in set_value — use resolve_path() first"
        raise ValueError(msg)

    parts = yaml_path.split("/")
    current = data

    for i, part in enumerate(parts):
        key, idx, filt = _parse_segment(part)
        is_last = i == len(parts) - 1

        if is_last:
            _set_terminal(current, key, idx, filt, value)
        else:
            current = _set_intermediate(current, key, idx, filt)

    return data


def delete_value(data: dict[str, Any], yaml_path: str) -> None:
    """Remove a key from a YAML dict at the given path.

    Navigates to the parent container and removes the terminal key.
    No-op if the path does not exist.  Does NOT support ``[*]`` wildcards.
    """
    if not yaml_path or "[*]" in yaml_path:
        return

    parts = yaml_path.split("/")
    current: Any = data

    # Navigate to the parent of the terminal key
    for part in parts[:-1]:
        key, idx, filt = _parse_segment(part)
        if not isinstance(current, dict):
            return
        current = current.get(key)
        if current is None:
            return
        if idx is not None:
            index = int(idx)
            if not isinstance(current, list) or index >= len(current):
                return
            current = current[index]

    # Remove the terminal key
    last_key, last_idx, _last_filt = _parse_segment(parts[-1])
    if isinstance(current, dict) and last_idx is None:
        current.pop(last_key, None)


def _set_terminal(
    current: dict[str, Any],
    key: str,
    idx: str | None,
    filt: str | None,
    value: Any,
) -> None:
    """Set a value at a terminal (last) segment."""
    if filt is not None:
        lst = current.setdefault(key, [])
        if not isinstance(lst, list):
            lst = []
            current[key] = lst
        _filter_set_terminal(lst, filt, value)
    elif idx is not None:
        index = int(idx)
        _ensure_list(current, key, index)
        current[key][index] = value
    else:
        current[key] = value


def _set_intermediate(
    current: dict[str, Any],
    key: str,
    idx: str | None,
    filt: str | None,
) -> dict[str, Any]:
    """Navigate through an intermediate (non-last) segment, creating as needed."""
    if filt is not None:
        lst = current.setdefault(key, [])
        if not isinstance(lst, list):
            lst = []
            current[key] = lst
        return _filter_ensure(lst, filt)

    if idx is not None:
        index = int(idx)
        _ensure_list(current, key, index)
        if not isinstance(current[key][index], dict):
            current[key][index] = {}
        return current[key][index]

    # Plain key
    if key not in current or not isinstance(current[key], dict):
        current[key] = {}
    return current[key]


def _filter_set_terminal(lst: list[Any], filt: str, value: Any) -> None:
    """Set a value at a filter-matched position (terminal segment)."""
    if "=" in filt:
        field, match_val = filt.split("=", 1)
        for i, item in enumerate(lst):
            if isinstance(item, dict) and str(item.get(field)) == match_val:
                lst[i] = value
                return
        # Not found — append with the identifying field
        if isinstance(value, dict):
            value.setdefault(field, match_val)
        lst.append(value)
    else:
        # {K} — set the value at dict[K]
        for i, item in enumerate(lst):
            if isinstance(item, str) and item == filt:
                if value is None:
                    return  # already a plain string, nothing to clear
                lst[i] = {filt: value}
                return
            if isinstance(item, dict) and filt in item:
                if value is None:
                    # Revert dict entry back to plain string
                    lst[i] = filt
                    return
                item[filt] = value
                return
        if value is not None:
            lst.append({filt: value})


def _filter_ensure(lst: list[Any], filt: str) -> dict[str, Any]:
    """Find or create a filter-matched entry and return the dict to descend into.

    {K}     → return dict[K] (promoting string entries, creating if needed)
    {F=V}   → return the matching dict (creating if needed)
    """
    if "=" in filt:
        field, match_val = filt.split("=", 1)
        for item in lst:
            if isinstance(item, dict) and str(item.get(field)) == match_val:
                return item
        new_item: dict[str, Any] = {field: match_val}
        lst.append(new_item)
        return new_item

    # {K} — dict-key filter in mixed list
    # First pass: prefer an existing dict entry (avoids duplicates when
    # the list contains both a plain string and a dict for the same key).
    for item in lst:
        if isinstance(item, dict) and filt in item:
            if not isinstance(item[filt], dict):
                item[filt] = {}
            # Remove any leftover plain string duplicate
            while filt in lst:
                lst.remove(filt)
            return item[filt]

    # Second pass: promote a plain string to a dict
    for i, item in enumerate(lst):
        if isinstance(item, str) and item == filt:
            promoted: dict[str, Any] = {filt: {}}
            lst[i] = promoted
            return promoted[filt]

    # Not found — create
    new_entry = {filt: {}}
    lst.append(new_entry)
    return new_entry[filt]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_list(container: dict[str, Any], key: str, index: int) -> None:
    """Ensure container[key] is a list with at least index+1 items."""
    if key not in container or not isinstance(container[key], list):
        container[key] = []
    lst = container[key]
    while len(lst) <= index:
        lst.append({})


def resolve_path(yaml_path: str, index: int | None = None) -> str:
    """Replace the first [*] wildcard with a concrete [index].

    resolve_path("users[*]/email", 2) -> "users[2]/email"
    resolve_path("a[*]/b[*]/c", 1) -> "a[1]/b[*]/c"  (only first)

    If index is None, returns path unchanged.
    """
    if index is None:
        return yaml_path
    return yaml_path.replace("[*]", f"[{index}]", 1)
