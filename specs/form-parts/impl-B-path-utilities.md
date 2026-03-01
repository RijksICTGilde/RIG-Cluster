# Sub-part B: Path Utilities

**Layer:** 0 (no dependencies)
**Files to create:**
- `opi/forms/editables/path.py`
- `tests/test_editables_path.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

YAML dict traversal using path strings. This is the most complex piece of the editables package — it needs to handle nested dicts, sequence indices, and wildcard expansion.

## Path Format

| Path | Resolves to |
|------|------------|
| `name` | `data["name"]` |
| `display-name` | `data["display-name"]` |
| `config/age-public-key` | `data["config"]["age-public-key"]` |
| `users[0]/email` | `data["users"][0]["email"]` |
| `users[*]/email` | `[u["email"] for u in data["users"]]` |
| `deployments[*]/components[*]/image` | nested list comprehension |

**Separator:** `/` (not `.`)
**Wildcards:** `[*]` means "all items in sequence"
**Concrete indices:** `[0]`, `[1]`, etc.
**Key names:** May contain hyphens (`display-name`, `age-public-key`)

---

## Functions

### get_value

```python
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
```

### set_value

```python
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
```

### resolve_path

```python
def resolve_path(yaml_path: str, index: int | None = None) -> str:
    """
    Replace the first [*] wildcard with a concrete [index].

    resolve_path("users[*]/email", 2) -> "users[2]/email"
    resolve_path("a[*]/b[*]/c", 1) -> "a[1]/b[*]/c"  (only first)

    If index is None, returns path unchanged.
    """
```

---

## Implementation Notes

### Segment parsing

Split the path on `/`, then parse each segment with regex:

```python
import re

_SEGMENT_RE = re.compile(r"^([^\[]+)(?:\[(\d+|\*)\])?$")
```

- Group 1: key name (may contain hyphens, dots, underscores)
- Group 2: `None` (plain key), integer string (concrete index), or `*` (wildcard)

### get_value algorithm

```
current = data
for each segment in path:
    parse segment -> (key, index_or_wildcard)

    if current is a dict:
        current = current.get(key)
        if current is None: return None
    else:
        return None  # can't traverse non-dict with a key

    if index_or_wildcard is None:
        continue  # plain key, already resolved
    elif index_or_wildcard == "*":
        if not isinstance(current, list): return None
        # Recurse: apply remaining path segments to each item
        remaining_path = join remaining segments
        if no remaining segments:
            return current  # the list itself
        return [get_value(item_as_dict_wrapper, remaining_path) for item in current]
    else:
        index = int(index_or_wildcard)
        if not isinstance(current, list) or index >= len(current): return None
        current = current[index]

return current
```

**Key subtlety for wildcards:** When encountering `[*]`, you need to apply the remaining path segments to each list item. This naturally handles nested wildcards like `deployments[*]/components[*]/image` through recursion.

### set_value algorithm

```
segments = parse all segments
walk data, creating intermediate dicts/lists as needed:

for i, (key, index) in enumerate(segments):
    if this is the last segment:
        if index is not None:
            ensure current[key] is a list
            extend list if needed
            current[key][index] = value
        else:
            current[key] = value
    else:
        if index is not None:
            ensure current[key] is a list
            extend list with empty dicts if needed
            current = current[key][index]
        else:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]
```

### Edge cases

- **Empty path (`""`):** `get_value` returns `data` itself. `set_value` raises ValueError.
- **Missing intermediate key:** `get_value` returns None. `set_value` creates intermediate dicts.
- **Index out of bounds:** `get_value` returns None. `set_value` extends the list with empty dicts.
- **Wildcard on non-list:** `get_value` returns None.
- **Wildcard in set_value:** Raises ValueError (use `resolve_path` first).
- **Nested wildcards in get_value:** Returns nested lists. `get_value(data, "a[*]/b[*]/c")` returns `[[c1, c2], [c3]]`.

---

## Tests: test_editables_path.py

This is the **most critical test file** — path utilities are the foundation for everything else.

```python
class TestGetValue:
    def test_simple_key(self):
        data = {"name": "test-project"}
        assert get_value(data, "name") == "test-project"

    def test_hyphenated_key(self):
        data = {"display-name": "Test Project"}
        assert get_value(data, "display-name") == "Test Project"

    def test_nested_dict(self):
        data = {"config": {"age-public-key": "age1abc..."}}
        assert get_value(data, "config/age-public-key") == "age1abc..."

    def test_deep_nesting(self):
        data = {"a": {"b": {"c": {"d": 42}}}}
        assert get_value(data, "a/b/c/d") == 42

    def test_concrete_index(self):
        data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        assert get_value(data, "users[0]/email") == "a@b.c"
        assert get_value(data, "users[1]/email") == "d@e.f"

    def test_wildcard_sequence(self):
        data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        assert get_value(data, "users[*]/email") == ["a@b.c", "d@e.f"]

    def test_nested_wildcards(self):
        data = {
            "deployments": [
                {"components": [{"image": "img1"}, {"image": "img2"}]},
                {"components": [{"image": "img3"}]},
            ]
        }
        result = get_value(data, "deployments[*]/components[*]/image")
        assert result == [["img1", "img2"], ["img3"]]

    def test_wildcard_no_remaining_path(self):
        data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        result = get_value(data, "users[*]")
        # Should return the list items themselves
        assert result == [{"email": "a@b.c"}, {"email": "d@e.f"}]

    def test_missing_key_returns_none(self):
        assert get_value({}, "nonexistent") is None

    def test_missing_nested_key_returns_none(self):
        data = {"config": {}}
        assert get_value(data, "config/missing-key") is None

    def test_missing_intermediate_returns_none(self):
        data = {"a": "not-a-dict"}
        assert get_value(data, "a/b/c") is None

    def test_index_out_of_bounds_returns_none(self):
        data = {"users": [{"email": "a@b.c"}]}
        assert get_value(data, "users[5]/email") is None

    def test_wildcard_on_non_list_returns_none(self):
        data = {"users": "not-a-list"}
        assert get_value(data, "users[*]/email") is None

    def test_empty_path_returns_data(self):
        data = {"name": "test"}
        assert get_value(data, "") == data

    def test_plain_key_returns_full_value(self):
        """A path to a dict returns the entire dict."""
        data = {"config": {"a": 1, "b": 2}}
        assert get_value(data, "config") == {"a": 1, "b": 2}

    def test_plain_key_returns_list(self):
        """A path to a list returns the entire list."""
        data = {"clusters": ["local", "staging"]}
        assert get_value(data, "clusters") == ["local", "staging"]


class TestSetValue:
    def test_simple_key(self):
        data = {}
        result = set_value(data, "name", "test")
        assert result["name"] == "test"

    def test_hyphenated_key(self):
        data = {}
        set_value(data, "display-name", "Test")
        assert data["display-name"] == "Test"

    def test_create_nested_intermediate(self):
        data = {}
        set_value(data, "config/age-public-key", "age1abc")
        assert data["config"]["age-public-key"] == "age1abc"

    def test_overwrite_existing(self):
        data = {"name": "old"}
        set_value(data, "name", "new")
        assert data["name"] == "new"

    def test_concrete_index_in_list(self):
        data = {"users": [{"email": "old@b.c"}]}
        set_value(data, "users[0]/email", "new@b.c")
        assert data["users"][0]["email"] == "new@b.c"

    def test_extend_list_for_index(self):
        data = {"users": [{"email": "a@b.c"}]}
        set_value(data, "users[2]/email", "c@d.e")
        assert len(data["users"]) == 3
        assert data["users"][2]["email"] == "c@d.e"

    def test_wildcard_raises_value_error(self):
        data = {"users": []}
        with pytest.raises(ValueError, match="wildcard"):
            set_value(data, "users[*]/email", "x@y.z")

    def test_returns_modified_data(self):
        data = {"a": 1}
        result = set_value(data, "b", 2)
        assert result is data  # Same dict, mutated in place
        assert result["b"] == 2

    def test_create_list_for_index(self):
        """If path has [0] but key doesn't exist, create list."""
        data = {}
        set_value(data, "users[0]/email", "a@b.c")
        assert isinstance(data["users"], list)
        assert data["users"][0]["email"] == "a@b.c"


class TestResolvePath:
    def test_single_wildcard(self):
        assert resolve_path("users[*]/email", 2) == "users[2]/email"

    def test_multiple_wildcards_first_only(self):
        assert resolve_path("a[*]/b[*]/c", 1) == "a[1]/b[*]/c"

    def test_no_wildcard_unchanged(self):
        assert resolve_path("name", 5) == "name"

    def test_none_index_unchanged(self):
        assert resolve_path("users[*]/email", None) == "users[*]/email"

    def test_zero_index(self):
        assert resolve_path("items[*]", 0) == "items[0]"
```

## Code Style

- Use lowercase type hints: `dict`, `list`
- Use `|` for unions: `str | None`
- Use `from __future__ import annotations`
- Keep functions pure where possible (get_value is pure, set_value mutates)
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
