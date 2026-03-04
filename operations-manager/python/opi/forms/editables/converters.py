from __future__ import annotations

from typing import Any

import yaml


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


class EnsureListConverter:
    """Coerces any scalar or None value to a list.

    Use on any editable whose YAML value is always a list but whose form
    transport may deliver a single string (e.g. HTMX checkbox_group with
    one item checked) or None (no items checked).
    """

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def view(self, value: Any) -> Any:
        return value


class ServiceListConverter:
    """Converts mixed str/dict service list to/from structured format."""

    def read(self, value: Any) -> list[str]:
        """Extract service names from mixed list."""
        from opi.services.services import ServiceAdapter

        if not value or not isinstance(value, list):
            return []
        return ServiceAdapter.extract_service_names_from_project_services(value)

    def write(self, value: Any) -> list[str | dict]:
        """Convert service names back to simple list (configs added separately).

        Handles both list input (multiple checkboxes) and single string
        (one checkbox checked, json-enc passes a scalar instead of array).
        """
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value:
            return [value]
        return []

    def view(self, value: Any) -> list[str]:
        """For display: just service names."""
        return self.read(value)


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


class IntegerConverter:
    """Converts a single integer to/from string for text input."""

    def read(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    def write(self, value: Any) -> int | None:
        if isinstance(value, int):
            return value
        val = str(value).strip()
        if val.isdigit():
            return int(val)
        return None

    def view(self, value: Any) -> str:
        return self.read(value)


class IntegerListConverter:
    """Converts list[int] to/from comma-separated string."""

    def read(self, value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any) -> list[int]:
        if isinstance(value, list):
            return [int(v) for v in value if str(v).strip().isdigit()]
        return [int(v.strip()) for v in str(value).split(",") if v.strip().isdigit()]

    def view(self, value: Any) -> str:
        return self.read(value)


class ListSingleSelectConverter:
    """Converts a YAML list to/from a single select value.

    Use this when the YAML stores a list (e.g. clusters: [local]) but the
    form should show a single-select dropdown.  Switching back to a
    multi-select widget later only requires removing this converter.
    """

    def read(self, value: Any) -> str:
        if isinstance(value, list) and value:
            return str(value[0])
        return ""

    def write(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return value
        if value:
            return [str(value)]
        return []

    def view(self, value: Any) -> str:
        return self.read(value)


class KeyValueConverter:
    """Converts between text (ENV or YAML) and structured dict.

    Supports two formats:
    - **ENV**: flat ``KEY=value`` lines (one per line, ``#`` comments allowed)
    - **YAML**: full YAML mapping (supports nested values, lists, etc.)

    The ``fmt`` parameter controls which format ``read()`` / ``view()``
    outputs.  ``write()`` auto-detects based on the current format.
    """

    def __init__(self, fmt: str = "env") -> None:
        self.fmt = fmt  # "env" or "yaml"

    def _has_complex_values(self, data: dict) -> bool:
        """Check if any values are non-scalar (lists, dicts)."""
        return any(isinstance(v, (list, dict)) for v in data.values())

    def read(self, value: Any) -> str:
        """Convert structured data back to editable text."""
        if isinstance(value, dict):
            # Use YAML format when values contain lists/dicts (ENV can't
            # represent those) or when the configured format is YAML.
            if self.fmt == "yaml" or self._has_complex_values(value):
                return yaml.dump(
                    value, default_flow_style=False, allow_unicode=True
                ).rstrip("\n")
            return "\n".join(f"{k}={v}" for k, v in value.items())
        return str(value or "")

    def write(self, value: Any) -> dict:
        """Parse editable text into a structured dict for storage."""
        if isinstance(value, dict):
            return value
        text = str(value or "").strip()
        if not text:
            return {}

        # Try YAML parse first — it handles both YAML and simple KEY: value
        try:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                return parsed
        except yaml.YAMLError:
            pass

        # Fallback: line-by-line ENV parsing
        result: dict[str, str] = {}
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                result[key.strip()] = val.strip()
        return result

    def view(self, value: Any) -> str:
        return self.read(value)


class ContainerImageConverter:
    """Lowercases container image references on write."""

    def read(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    def write(self, value: Any) -> str:
        if not value:
            return ""
        return str(value).strip().lower()

    def view(self, value: Any) -> str:
        return self.read(value)


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


class AGEEncryptConverter:
    """Encrypts/decrypts field values using AGE encryption.

    Uses the system AGE public key for encryption and the AGE private key
    for decryption. Displays masked values in view mode.

    Wraps ``opi.utils.age.encrypt_age_content_sync`` and
    ``opi.utils.age.decrypt_age_content`` for the converter protocol.
    """

    def __init__(self, public_key: str | None = None) -> None:
        self._public_key = public_key

    def _get_public_key(self) -> str:
        if self._public_key:
            return self._public_key
        from opi.core.config import settings

        return settings.SOPS_AGE_PUBLIC_KEY

    def read(self, value: Any) -> str:
        """Decrypt AGE-encrypted value for form editing."""
        if not value or not isinstance(value, str):
            return ""
        if "BEGIN AGE ENCRYPTED FILE" not in value:
            return value
        try:
            from opi.utils.age import decrypt_age_content

            return decrypt_age_content(value)
        except Exception:
            return ""

    def write(self, value: Any) -> str:
        """Encrypt value with AGE before YAML storage."""
        if not value:
            return ""
        value_str = str(value).strip()
        if not value_str:
            return ""
        if "BEGIN AGE ENCRYPTED FILE" in value_str:
            return value_str
        try:
            from opi.utils.age import encrypt_age_content_sync

            return encrypt_age_content_sync(value_str, self._get_public_key())
        except Exception:
            return value_str

    def view(self, value: Any) -> str:
        """Masked display — never show encrypted content in UI."""
        if not value:
            return "Niet geconfigureerd"
        if isinstance(value, str) and "BEGIN AGE ENCRYPTED FILE" in value:
            return "********"
        return "********"
