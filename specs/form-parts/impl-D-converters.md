# Sub-part D: Converters

**Layer:** 1 (depends on Sub-part A for protocol definitions)
**Files to create:**
- `opi/forms/editables/converters.py`
- `tests/test_editables_converters.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

9 sync converters implementing the `EditableConverter` protocol from `opi/forms/editables/editable.py`.

**IMPORTANT:** These are **synchronous** converters (no async/await). They are different from the existing async `Converter` protocol in `opi/forms/field.py`. The editable converters have three methods: `read()`, `write()`, `view()`.

```python
class EditableConverter(Protocol):
    def read(self, value: Any) -> Any:    # YAML -> form input value
    def write(self, value: Any) -> Any:   # form submission -> YAML storage value
    def view(self, value: Any) -> Any:    # YAML -> read-only display value
```

## Converters

### EncryptedDisplayConverter

For read-only display of AGE-encrypted fields. Never exposes actual encrypted content.

```python
class EncryptedDisplayConverter:
    """Displays encrypted fields as status indicators, not actual values."""

    def read(self, value: Any) -> str:
        """For form inputs — not used since field is readonly."""
        return ""

    def write(self, value: Any) -> Any:
        """Never writes — field is readonly. Preserves original."""
        return value

    def view(self, value: Any) -> str:
        """Status message for display-card widget."""
        if value and isinstance(value, str) and "BEGIN AGE ENCRYPTED FILE" in value:
            return "Versleuteld opgeslagen"
        if value:
            return "Geconfigureerd"
        return "Niet geconfigureerd"
```

### TruncateConverter

Truncates long values for display (e.g., AGE public keys).

```python
class TruncateConverter:
    """Truncates values for display, showing first N characters + '...'."""

    def __init__(self, max_length: int = 20) -> None:
        self.max_length = max_length

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value

    def view(self, value: Any) -> str:
        if not value:
            return "Niet geconfigureerd"
        value_str = str(value)
        if len(value_str) > self.max_length:
            return value_str[: self.max_length] + "..."
        return value_str
```

### ServiceListConverter

Handles the mixed string/dict format used for services in project YAML:

```yaml
services:
  - publish-on-web                          # simple string
  - keycloak:                               # dict with config
      config:
        template: sso-support
```

```python
class ServiceListConverter:
    """Converts mixed str/dict service list to/from structured format."""

    def read(self, value: Any) -> list[str]:
        """Extract service names from mixed list."""
        from opi.services.services import ServiceAdapter
        if not value or not isinstance(value, list):
            return []
        return ServiceAdapter.extract_service_names_from_project_services(value)

    def write(self, value: Any) -> list[str | dict]:
        """Convert service names back to simple list (configs added separately)."""
        if isinstance(value, list):
            return value
        return []

    def view(self, value: Any) -> list[str]:
        """For display: just service names."""
        return self.read(value)
```

### NewlineSeparatedListConverter

For textarea fields that represent lists (e.g., Keycloak redirect URIs).

```python
class NewlineSeparatedListConverter:
    """Converts list to/from newline-separated string."""

    def read(self, value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return value
        return [line.strip() for line in str(value).split("\n") if line.strip()]

    def view(self, value: Any) -> str:
        return self.read(value)
```

### IntegerListConverter

For comma-separated integer fields (e.g., ports).

```python
class IntegerListConverter:
    """Converts list[int] to/from comma-separated string."""

    def read(self, value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any) -> list[int]:
        if isinstance(value, list):
            return [int(v) for v in value if str(v).strip().isdigit()]
        return [
            int(v.strip())
            for v in str(value).split(",")
            if v.strip().isdigit()
        ]

    def view(self, value: Any) -> str:
        return self.read(value)
```

### KeyValueConverter

For textarea fields representing KEY=VALUE pairs (e.g., aliases, env vars).

```python
class KeyValueConverter:
    """Converts dict to/from KEY=VALUE text format."""

    def read(self, value: Any) -> str:
        if isinstance(value, dict):
            return "\n".join(f"{k}={v}" for k, v in value.items())
        return str(value or "")

    def write(self, value: Any) -> dict[str, str]:
        if isinstance(value, dict):
            return value
        result: dict[str, str] = {}
        for line in str(value).split("\n"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                result[key.strip()] = val.strip()
        return result

    def view(self, value: Any) -> str:
        return self.read(value)
```

### CloneFromDisplayConverter

For read-only display of clone-from metadata on deployments.

```python
class CloneFromDisplayConverter:
    """Formats clone-from metadata for display."""

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value

    def view(self, value: Any) -> str:
        if not value or not isinstance(value, dict):
            return ""
        reference = value.get("reference", "onbekend")
        clone_type = value.get("type", "onbekend")
        status = value.get("status", {})
        if status.get("completed"):
            timestamp = status.get("timestamp", "")
            return f"Gekloond van {reference} ({clone_type}) — Voltooid op {timestamp}"
        return f"Gekloond van {reference} ({clone_type}) — Bezig..."
```

### DeploymentServicesDisplayConverter

For read-only display of deployment-level service overrides.

```python
class DeploymentServicesDisplayConverter:
    """Formats deployment-level service overrides for display."""

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value

    def view(self, value: Any) -> str:
        if not value or not isinstance(value, list):
            return "Geen deployment services"
        names = [s.get("reference", "onbekend") for s in value if isinstance(s, dict)]
        return ", ".join(names) if names else "Geen deployment services"
```

### KeycloakRealmsDisplayConverter

For read-only display of Keycloak realm configurations.

```python
class KeycloakRealmsDisplayConverter:
    """Formats keycloak realm list for display."""

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value

    def view(self, value: Any) -> list[dict[str, str]]:
        """Returns structured data for template rendering."""
        if not value or not isinstance(value, list):
            return []
        return [
            {
                "host": kc.get("host", ""),
                "realm": kc.get("realm", ""),
                "username": kc.get("username", ""),
            }
            for kc in value
            if isinstance(kc, dict)
        ]
```

---

## Tests: test_editables_converters.py

```python
class TestEncryptedDisplayConverter:
    def test_view_age_encrypted(self):
        conv = EncryptedDisplayConverter()
        assert conv.view("-----BEGIN AGE ENCRYPTED FILE-----\ndata") == "Versleuteld opgeslagen"

    def test_view_plain_value(self):
        assert EncryptedDisplayConverter().view("some-value") == "Geconfigureerd"

    def test_view_none(self):
        assert EncryptedDisplayConverter().view(None) == "Niet geconfigureerd"

    def test_view_empty_string(self):
        assert EncryptedDisplayConverter().view("") == "Niet geconfigureerd"

    def test_read_returns_empty(self):
        assert EncryptedDisplayConverter().read("anything") == ""

    def test_write_preserves_original(self):
        original = "-----BEGIN AGE ENCRYPTED FILE-----\ndata"
        assert EncryptedDisplayConverter().write(original) == original


class TestTruncateConverter:
    def test_view_long_string(self):
        conv = TruncateConverter(max_length=10)
        assert conv.view("abcdefghijklmnop") == "abcdefghij..."

    def test_view_short_string(self):
        conv = TruncateConverter(max_length=20)
        assert conv.view("short") == "short"

    def test_view_none(self):
        assert TruncateConverter().view(None) == "Niet geconfigureerd"

    def test_read_write_passthrough(self):
        conv = TruncateConverter()
        assert conv.read("value") == "value"
        assert conv.write("value") == "value"


class TestServiceListConverter:
    def test_read_mixed_list(self):
        value = ["publish-on-web", {"keycloak": {"config": {"template": "sso-support"}}}]
        result = ServiceListConverter().read(value)
        assert "publish-on-web" in result
        assert "keycloak" in result

    def test_read_empty(self):
        assert ServiceListConverter().read(None) == []
        assert ServiceListConverter().read([]) == []

    def test_write_simple_list(self):
        assert ServiceListConverter().write(["a", "b"]) == ["a", "b"]

    def test_view_matches_read(self):
        value = ["publish-on-web"]
        conv = ServiceListConverter()
        assert conv.view(value) == conv.read(value)


class TestIntegerListConverter:
    def test_read_list_to_string(self):
        assert IntegerListConverter().read([80, 443]) == "80, 443"

    def test_write_string_to_list(self):
        assert IntegerListConverter().write("80, 443") == [80, 443]

    def test_round_trip(self):
        conv = IntegerListConverter()
        original = [8000, 8080, 443]
        assert conv.write(conv.read(original)) == original

    def test_write_skips_invalid(self):
        assert IntegerListConverter().write("80, abc, 443") == [80, 443]

    def test_read_empty_list(self):
        assert IntegerListConverter().read([]) == ""

    def test_read_none(self):
        assert IntegerListConverter().read(None) == ""


class TestKeyValueConverter:
    def test_read_dict_to_string(self):
        result = KeyValueConverter().read({"KEY": "value", "OTHER": "val2"})
        assert "KEY=value" in result
        assert "OTHER=val2" in result

    def test_write_string_to_dict(self):
        result = KeyValueConverter().write("KEY=value\nOTHER=val2")
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_skips_comments(self):
        result = KeyValueConverter().write("# comment\nKEY=value")
        assert result == {"KEY": "value"}

    def test_write_skips_empty_lines(self):
        result = KeyValueConverter().write("KEY=value\n\nOTHER=val2")
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_handles_equals_in_value(self):
        result = KeyValueConverter().write("KEY=val=ue")
        assert result == {"KEY": "val=ue"}


class TestCloneFromDisplayConverter:
    def test_view_completed(self):
        value = {"reference": "prod", "type": "remote-source", "status": {"completed": True, "timestamp": "2026-02-03"}}
        result = CloneFromDisplayConverter().view(value)
        assert "prod" in result
        assert "Voltooid" in result

    def test_view_in_progress(self):
        value = {"reference": "prod", "type": "remote-source", "status": {}}
        result = CloneFromDisplayConverter().view(value)
        assert "Bezig" in result

    def test_view_none(self):
        assert CloneFromDisplayConverter().view(None) == ""


class TestDeploymentServicesDisplayConverter:
    def test_view_with_services(self):
        value = [{"reference": "minio-storage"}, {"reference": "redis"}]
        result = DeploymentServicesDisplayConverter().view(value)
        assert "minio-storage" in result
        assert "redis" in result

    def test_view_empty(self):
        assert DeploymentServicesDisplayConverter().view([]) == "Geen deployment services"
        assert DeploymentServicesDisplayConverter().view(None) == "Geen deployment services"


class TestKeycloakRealmsDisplayConverter:
    def test_view_with_realms(self):
        value = [{"host": "https://kc.example.nl", "realm": "my-realm", "username": "admin"}]
        result = KeycloakRealmsDisplayConverter().view(value)
        assert len(result) == 1
        assert result[0]["realm"] == "my-realm"

    def test_view_empty(self):
        assert KeycloakRealmsDisplayConverter().view(None) == []
        assert KeycloakRealmsDisplayConverter().view([]) == []
```

## Code Style

- Use lowercase type hints: `dict`, `list`
- Use `|` for unions: `str | None`
- Use `from __future__ import annotations`
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
