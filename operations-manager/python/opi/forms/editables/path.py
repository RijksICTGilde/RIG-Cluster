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
    The list is a mix of strings and dicts.  Finds the entry identified by *K*
    and descends into its body.  Two entry forms carry a body, and both have to be
    understood: the legacy single-key dict ``{K: {…}}``, whose body is ``dict[K]``,
    and the uniform record ``{name|reference: K, …}``, which IS its own body. Match
    only the legacy form and an entry already normalised to a record reads back as
    empty, so the form shows a default where a stored value is.
    On write, promotes a matching string to ``{K: {}}`` or appends
    a new entry if not found; an existing record is written into as-is.

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


def get_value(data: dict[str, Any], yaml_path: str) -> Any:
    """Extract the value at *yaml_path*; missing data gives None rather than an exception.

    A wildcard ``[*]`` returns the list of all matches.
    """
    if yaml_path == "":
        return data
    return _get_recursive(data, yaml_path.split("/"))


def _get_recursive(current: Any, parts: list[str]) -> Any:
    for i, part in enumerate(parts):
        key, idx, filt = _parse_segment(part)

        if not isinstance(current, dict):
            return None

        current = current.get(key)
        if current is None:
            return None

        remaining = parts[i + 1 :]

        if idx == "*":
            if not isinstance(current, list):
                return None
            if not remaining:
                return list(current)
            return [_get_recursive(item, remaining) for item in current]

        if idx is not None:
            index = int(idx)
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
            continue

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

    {K}     → find the entry identified by K, return its body
    {F=V}   → find dict where dict[F] == V, return that dict
    """
    if "=" in filt:
        field, value = filt.split("=", 1)
        for item in items:
            if isinstance(item, dict) and str(item.get(field)) == value:
                return item
        return None

    # {K}: an identity filter in a mixed list, in both entry forms.
    for item in items:
        if not isinstance(item, dict):
            continue
        if _is_record_for(item, filt):
            return item
        if filt in item:
            return item[filt]
    return None


def _is_record_for(item: dict[str, Any], name: str) -> bool:
    """Whether ``item`` is the uniform record ``{name|reference: name, …}``.

    The record form carries its body on the entry itself, so a matching record is
    descended into directly rather than through a name key.
    """
    return item.get("name") == name or item.get("reference") == name


def set_value(data: dict[str, Any], yaml_path: str, value: Any) -> dict[str, Any]:
    """Set a value at *yaml_path*, mutating *data* in place and returning it.

    Creates intermediate dicts, extends lists, and promotes or creates entries for filter
    expressions as needed. A ``[*]`` wildcard is not supported: resolve it with
    ``resolve_path()`` first.
    """
    if yaml_path == "":
        msg = "Cannot set value at empty path"
        raise ValueError(msg)
    if "[*]" in yaml_path:
        msg = "Cannot use wildcard [*] in set_value - use resolve_path() first"
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
    """Remove the key at *yaml_path*; a no-op when it does not exist.

    A ``[*]`` wildcard is not supported.
    """
    if not yaml_path or "[*]" in yaml_path:
        return

    parts = yaml_path.split("/")
    current: Any = data

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
        elif filt is not None:
            # A filter segment has to be followed here too. Ignore it and navigation walks
            # into the LIST, the next segment finds no dict, and every delete through a
            # ``services{X}/config/...`` path is a no-op: ``remove_when_none`` then leaves
            # the old value in the file.
            if not isinstance(current, list):
                return
            current = _filter_get(current, filt)
            if current is None:
                return

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
        # Not found, so append it with the identifying field.
        if isinstance(value, dict):
            value.setdefault(field, match_val)
        lst.append(value)
    else:
        # {K}: set the value on the entry identified by filt.
        for i, item in enumerate(lst):
            if isinstance(item, dict) and _is_record_for(item, filt):
                # A record IS its own body, so replacing it would drop its identity: a
                # terminal write onto one merges when the value is a mapping.
                if value is None:
                    lst[i] = filt
                elif isinstance(value, dict):
                    item.update(value)
                else:
                    lst[i] = {filt: value}
                return
            if isinstance(item, str) and item == filt:
                if value is None:
                    return  # already a plain string, nothing to clear
                lst[i] = {filt: value}
                return
            if isinstance(item, dict) and filt in item:
                if value is None:
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

    # {K}: an identity filter in a mixed list. An existing uniform record is its own body,
    # so descend into it rather than adding a second entry beside it under the name key.
    for item in lst:
        if isinstance(item, dict) and _is_record_for(item, filt):
            while filt in lst:
                lst.remove(filt)
            return item

    # Prefer an existing dict entry: the list can hold both a plain string and a dict for
    # the same key.
    for item in lst:
        if isinstance(item, dict) and filt in item:
            if not isinstance(item[filt], dict):
                item[filt] = {}
            while filt in lst:
                lst.remove(filt)
            return item[filt]

    # Otherwise promote a plain string to a dict.
    for i, item in enumerate(lst):
        if isinstance(item, str) and item == filt:
            promoted: dict[str, Any] = {filt: {}}
            lst[i] = promoted
            return promoted[filt]

    new_entry = {filt: {}}
    lst.append(new_entry)
    return new_entry[filt]


def _ensure_list(container: dict[str, Any], key: str, index: int) -> None:
    """Ensure container[key] is a list with at least index+1 items."""
    if key not in container or not isinstance(container[key], list):
        container[key] = []
    lst = container[key]
    while len(lst) <= index:
        lst.append({})


def resolve_path(yaml_path: str, index: int | None = None) -> str:
    """Replace the FIRST [*] wildcard with a concrete [index]; None leaves the path alone.

    resolve_path("users[*]/email", 2) -> "users[2]/email"
    resolve_path("a[*]/b[*]/c", 1) -> "a[1]/b[*]/c"
    """
    if index is None:
        return yaml_path
    return yaml_path.replace("[*]", f"[{index}]", 1)
