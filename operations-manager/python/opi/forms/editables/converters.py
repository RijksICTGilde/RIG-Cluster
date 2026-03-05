from __future__ import annotations

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)


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
    """Converts KEY=value text to/from YAML storage format.

    Two write modes controlled by ``write_as``:

    - ``"dict"`` (default): Parses ``KEY=value`` text into a dict.
      Used for ``aliases`` which are stored as YAML maps.
    - ``"string"``: Keeps the raw text as a string literal.
      Used for ``user-env-vars`` which are stored as a string
      (and later AGE-encrypted by a generator).
    """

    def __init__(self, fmt: str = "env", write_as: str = "dict") -> None:
        self.fmt = fmt  # "env" or "yaml"
        self.write_as = write_as  # "dict" or "string"

    def read(self, value: Any) -> str:
        """Return the stored text for display in the editor."""
        logger.info(
            "[KeyValueConverter.read] write_as=%s, input type=%s, value=%r", self.write_as, type(value).__name__, value
        )
        if isinstance(value, dict):
            if not value:
                return ""
            # Stored as dict — convert to KEY: value text for the editor.
            # dict() strips ruamel CommentedMap so stdlib yaml.dump works.
            return yaml.dump(dict(value), default_flow_style=False, allow_unicode=True).rstrip("\n")
        return str(value or "")

    def write(self, value: Any) -> dict[str, str] | str | None:
        """Convert form input to the appropriate YAML storage format.

        Returns None for empty input so the YAML key is omitted.
        """
        logger.info(
            "[KeyValueConverter.write] write_as=%s, input type=%s, value=%r", self.write_as, type(value).__name__, value
        )
        result = self._write_as_string(value) if self.write_as == "string" else self._write_as_dict(value)
        logger.info(
            "[KeyValueConverter.write] result type=%s, result=%r",
            type(result).__name__ if result is not None else "None",
            result,
        )
        return result

    def _write_as_dict(self, value: Any) -> dict[str, str] | None:
        """Parse KEY=value text into a dict for YAML map storage."""
        if isinstance(value, dict):
            return value if value else None
        text = str(value or "").strip()
        if not text:
            return None
        return self._parse_env_text(text)

    @staticmethod
    def _write_as_string(value: Any) -> str | None:
        """Return raw text as a string for YAML literal scalar storage."""
        if isinstance(value, dict):
            if not value:
                return None
            # Convert dict back to KEY=value text
            return "\n".join(f"{k}={v}" for k, v in value.items())
        text = str(value or "").strip()
        return text if text else None

    @staticmethod
    def _parse_env_text(text: str) -> dict[str, str]:
        """Parse ``KEY=value`` lines into a dict."""
        result: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key, _, val = stripped.partition("=")
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

    Uses the system AGE public key for encryption and the system AGE
    private key for decryption. Displays masked values in view mode.

    For fields encrypted with the **project** key (e.g. ``user-env-vars``),
    use a generator instead — converters do not have access to project keys.
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
            from opi.core.config import settings
            from opi.utils.age import decrypt_age_content_sync

            private_key = settings.SOPS_AGE_PRIVATE_KEY
            if not private_key:
                return ""
            decrypted = decrypt_age_content_sync(value, private_key)
            return decrypted if decrypted is not None else ""
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
