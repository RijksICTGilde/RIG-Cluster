from __future__ import annotations

from typing import Any


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
