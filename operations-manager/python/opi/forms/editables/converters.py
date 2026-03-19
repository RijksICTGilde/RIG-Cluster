from __future__ import annotations

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class EncryptedDisplayConverter:
    """Displays encrypted fields as status indicators, not actual values."""

    def read(self, value: Any) -> str:
        """For form inputs - not used since field is readonly."""
        return ""

    def write(self, value: Any) -> Any:
        """Never writes - field is readonly. Preserves original."""
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


class EmptyToNoneConverter:
    """Maps empty strings to None so the YAML key is omitted.

    Use on optional select fields where the empty option (value="")
    should result in the key being absent from the project file.
    """

    def read(self, value: Any) -> Any:
        return value or ""

    def write(self, value: Any) -> Any:
        if not value:
            return None
        return value

    def view(self, value: Any) -> str:
        return str(value) if value else ""


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

        When a storage service is checked, ``json-enc.js`` promotes the
        string entry to a dict (``{"persistent-storage": {"config": ...}}``).
        These dicts are kept as-is so storage config is preserved.
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

    When the stored value is AGE-encrypted, ``read()`` and ``view()``
    auto-detect the encryption and decrypt transparently using the
    project's private key (resolved from ``yaml_data``).
    """

    def __init__(self, fmt: str = "env", write_as: str = "dict") -> None:
        self.fmt = fmt  # "env" or "yaml"
        self.write_as = write_as  # "dict" or "string"

    def read(self, value: Any, yaml_data: dict[str, Any] | None = None) -> str:
        """Return the stored text for display in the editor.

        If the value is AGE-encrypted and ``yaml_data`` is provided,
        the value is decrypted first using the project's private key.
        """
        logger.info(
            "[KeyValueConverter.read] write_as=%s, input type=%s, value=%r", self.write_as, type(value).__name__, value
        )
        value = self._maybe_decrypt(value, yaml_data)
        if isinstance(value, dict):
            if not value:
                return ""
            if self.fmt == "env":
                return "\n".join(f"{k}={v}" for k, v in value.items())
            # YAML format: KEY: value text for the editor.
            # dict() strips ruamel CommentedMap so stdlib yaml.dump works.
            return yaml.dump(dict(value), default_flow_style=False, allow_unicode=True).rstrip("\n")
        return str(value or "")

    def write(self, value: Any, yaml_data: dict[str, Any] | None = None) -> dict[str, str] | str | None:
        """Convert form input to the appropriate YAML storage format.

        Returns None for empty input so the YAML key is omitted.
        When ``write_as="string"`` and *yaml_data* contains a project
        AGE public key, the result is AGE-encrypted automatically.
        """
        logger.info(
            "[KeyValueConverter.write] write_as=%s, input type=%s, value=%r", self.write_as, type(value).__name__, value
        )
        result = self._write_as_string(value) if self.write_as == "string" else self._write_as_dict(value)
        if result and self.write_as == "string" and isinstance(result, str):
            result = self._maybe_encrypt(result, yaml_data)
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

    @staticmethod
    def _maybe_decrypt(value: Any, yaml_data: dict[str, Any] | None) -> Any:
        """Decrypt AGE-encrypted value using the project's private key.

        Returns the original value unchanged when:
        - value is not a string or not AGE-encrypted
        - yaml_data is not provided (e.g. during wizard flow with plaintext data)
        - decryption fails for any reason
        """
        if not isinstance(value, str) or "BEGIN AGE ENCRYPTED FILE" not in value:
            return value
        if not yaml_data:
            return value
        try:
            from opi.core.config import settings
            from opi.utils.age import decrypt_age_content_sync

            system_private_key = settings.SOPS_AGE_PRIVATE_KEY
            if not system_private_key:
                logger.warning("[KeyValueConverter] No system AGE private key available")
                return value
            encoded_project_key = yaml_data.get("config", {}).get("age-private-key")
            if not encoded_project_key:
                logger.warning("[KeyValueConverter] No project age-private-key in yaml_data")
                return value
            project_private_key = decrypt_age_content_sync(encoded_project_key, system_private_key)
            if not project_private_key:
                logger.warning("[KeyValueConverter] Failed to decrypt project private key")
                return value
            decrypted = decrypt_age_content_sync(value, project_private_key)
            if decrypted is not None:
                logger.debug("[KeyValueConverter] Successfully decrypted AGE-encrypted value")
                return decrypted
            return value
        except Exception:
            logger.warning("[KeyValueConverter] AGE decryption failed, returning raw value", exc_info=True)
            return value

    @staticmethod
    def _maybe_encrypt(value: str, yaml_data: dict[str, Any] | None) -> str:
        """Encrypt a plain-text value using the project's AGE public key.

        Returns the original value unchanged when:
        - value is already AGE-encrypted
        - yaml_data is not provided or has no project public key
        - encryption fails
        """
        if "BEGIN AGE ENCRYPTED FILE" in value:
            return value
        if not yaml_data:
            return value
        try:
            from ruamel.yaml.scalarstring import LiteralScalarString

            from opi.utils.age import encrypt_age_content_sync

            public_key = yaml_data.get("config", {}).get("age-public-key")
            if not public_key:
                logger.debug("[KeyValueConverter] No project AGE public key, skipping encryption")
                return value
            encrypted = encrypt_age_content_sync(value, public_key)
            logger.debug("[KeyValueConverter] Encrypted user-env-vars with project AGE key")
            return LiteralScalarString(encrypted)
        except Exception:
            logger.warning("[KeyValueConverter] AGE encryption failed, returning plain value", exc_info=True)
            return value

    def view(self, value: Any, yaml_data: dict[str, Any] | None = None) -> str:
        return self.read(value, yaml_data=yaml_data)


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


class CloneFromConverter:
    """Converts between the clone-from dict format and a simple deployment name string.

    YAML format (what the managers expect):
        clone-from:
          type: deployment
          reference: staging
          mode: once

    Form format (what the dropdown produces):
        "staging"  (or "" for no clone)
    """

    def read(self, value: Any) -> Any:
        """Dict → string for form display."""
        if isinstance(value, dict):
            return value.get("reference", "")
        if isinstance(value, str):
            return value
        return ""

    def write(self, value: Any) -> Any:
        """String → dict for YAML storage."""
        if not value or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, dict):
            return value
        return {
            "type": "deployment",
            "reference": str(value),
            "mode": "once",
        }

    def view(self, value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, dict):
            reference = value.get("reference", "onbekend")
            clone_type = value.get("type", "onbekend")
            status = value.get("status", {})
            if status.get("completed"):
                timestamp = status.get("timestamp", "")
                return f"Gekloond van {reference} ({clone_type}) - Voltooid op {timestamp}"
            return f"Gekloond van {reference} ({clone_type}) - Bezig..."
        return str(value)


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


class CustomDomainSelectConverter:
    """Maps non-standard base-domain values to '__custom__' for the select widget."""

    def read(self, value: Any) -> Any:
        return self.view(value)

    def write(self, value: Any) -> Any:
        return value  # passthrough - merge happens in post-processing

    def view(self, value: Any) -> Any:
        if not value:
            return value
        from opi.connectors.subdomain import get_supported_base_domains
        from opi.core.config import settings

        if str(value) not in get_supported_base_domains(cluster=settings.CLUSTER_MANAGER):
            return "__custom__"
        return value


class AGEEncryptConverter:
    """Encrypts/decrypts field values using AGE encryption.

    Uses the system AGE public key for encryption and the system AGE
    private key for decryption. Displays masked values in view mode.

    For fields encrypted with the **project** key (e.g. ``user-env-vars``),
    use a generator instead - converters do not have access to project keys.
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
        """Masked display - never show encrypted content in UI."""
        if not value:
            return "Niet geconfigureerd"
        if isinstance(value, str) and "BEGIN AGE ENCRYPTED FILE" in value:
            return "********"
        return "********"
