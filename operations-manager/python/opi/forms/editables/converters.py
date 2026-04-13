from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from opi.core.rrule_utils import build_rrule, format_rrule, parse_rrule
from opi.forms.editables.service_path import smart_get_value

logger = logging.getLogger(__name__)


class EncryptedDisplayConverter:
    """Displays encrypted fields as status indicators, not actual values."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """For form inputs - not used since field is readonly."""
        return ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        """Never writes - field is readonly. Preserves original."""
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value or ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        if not value:
            return None
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return str(value) if value else ""


class EnsureListConverter:
    """Coerces any scalar or None value to a list.

    Use on any editable whose YAML value is always a list but whose form
    transport may deliver a single string (e.g. HTMX checkbox_group with
    one item checked) or None (no items checked).
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value


class ServiceListConverter:
    """Converts mixed str/dict service list to/from structured format."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:
        """Extract service names from mixed list."""
        from opi.services.services import ServiceAdapter

        if not value or not isinstance(value, list):
            return []
        return ServiceAdapter.extract_service_names_from_project_services(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str | dict]:
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

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:
        """For display: just service names."""
        return self.read(value)


class NewlineSeparatedListConverter:
    """Converts list to/from newline-separated string."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if isinstance(value, list):
            return "\n".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:
        if isinstance(value, list):
            return value
        return [line.strip() for line in str(value).split("\n") if line.strip()]

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value)


class IntegerConverter:
    """Converts a single integer to/from string for text input."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if value is None:
            return ""
        return str(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> int | None:
        if isinstance(value, int):
            return value
        val = str(value).strip()
        if val.isdigit():
            return int(val)
        return None

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value)


class IntegerListConverter:
    """Converts list[int] to/from comma-separated string."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[int]:
        if isinstance(value, list):
            return [int(v) for v in value if str(v).strip().isdigit()]
        return [int(v.strip()) for v in str(value).split(",") if v.strip().isdigit()]

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value)


class ListSingleSelectConverter:
    """Converts a YAML list to/from a single select value.

    Use this when the YAML stores a list (e.g. clusters: [local]) but the
    form should show a single-select dropdown.  Switching back to a
    multi-select widget later only requires removing this converter.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if isinstance(value, list) and value:
            return str(value[0])
        return ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:
        if isinstance(value, list):
            return value
        if value:
            return [str(value)]
        return []

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
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
    project's private key (resolved from ``context_data``).
    """

    def __init__(self, fmt: str = "env", write_as: str = "dict") -> None:
        self.fmt = fmt  # "env" or "yaml"
        self.write_as = write_as  # "dict" or "string"

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """Return the stored text for display in the editor.

        If the value is AGE-encrypted and ``context_data`` is provided,
        the value is decrypted first using the project's private key.
        """
        logger.info(
            "[KeyValueConverter.read] write_as=%s, input type=%s, value=%r", self.write_as, type(value).__name__, value
        )
        value = self._maybe_decrypt(value, context_data)
        if isinstance(value, dict):
            if not value:
                return ""
            if self.fmt == "env":
                return "\n".join(f"{k}={v}" for k, v in value.items())
            # YAML format: KEY: value text for the editor.
            # dict() strips ruamel CommentedMap so stdlib yaml.dump works.
            return yaml.dump(dict(value), default_flow_style=False, allow_unicode=True).rstrip("\n")
        return str(value or "")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> dict[str, str] | str | None:
        """Convert form input to the appropriate YAML storage format.

        Returns None for empty input so the YAML key is omitted.
        When ``write_as="string"`` and *context_data* contains a project
        AGE public key, the result is AGE-encrypted automatically.
        """
        logger.info(
            "[KeyValueConverter.write] write_as=%s, input type=%s, value=%r", self.write_as, type(value).__name__, value
        )
        result = self._write_as_string(value) if self.write_as == "string" else self._write_as_dict(value)
        if result and self.write_as == "string" and isinstance(result, str):
            result = self._maybe_encrypt(result, context_data)
        logger.info(
            "[KeyValueConverter.write] result type=%s, result=%r",
            type(result).__name__ if result is not None else "None",
            result,
        )
        return result

    def _write_as_dict(self, value: Any) -> dict[str, str] | None:
        """Parse KEY=value text into a dict for YAML map storage."""
        if isinstance(value, dict):
            return value or None
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
        return text or None

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
    def _maybe_decrypt(value: Any, context_data: dict[str, Any] | None) -> Any:
        """Decrypt AGE-encrypted value using the project's private key."""
        if not isinstance(value, str) or "BEGIN AGE ENCRYPTED FILE" not in value:
            return value
        if not context_data:
            return value
        try:
            from opi.core.config import settings
            from opi.utils.age import decrypt_age_content_sync

            system_private_key = settings.SOPS_AGE_PRIVATE_KEY
            if not system_private_key:
                logger.warning("[KeyValueConverter] No system AGE private key available")
                return value
            encoded_project_key = context_data.get("config", {}).get("age-private-key")
            if not encoded_project_key:
                logger.warning("[KeyValueConverter] No project age-private-key in context_data")
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
    def _maybe_encrypt(value: str, context_data: dict[str, Any] | None) -> str:
        """Encrypt a plain-text value using the project's AGE public key."""
        if "BEGIN AGE ENCRYPTED FILE" in value:
            return value
        if not context_data:
            return value
        try:
            from ruamel.yaml.scalarstring import LiteralScalarString

            from opi.utils.age import encrypt_age_content_sync

            public_key = context_data.get("config", {}).get("age-public-key")
            if not public_key:
                logger.debug("[KeyValueConverter] No project AGE public key, skipping encryption")
                return value
            encrypted = encrypt_age_content_sync(value, public_key)
            logger.debug("[KeyValueConverter] Encrypted user-env-vars with project AGE key")
            return LiteralScalarString(encrypted)
        except Exception:
            logger.warning("[KeyValueConverter] AGE encryption failed, returning plain value", exc_info=True)
            return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value, context_data=context_data)


class ContainerImageConverter:
    """Lowercases container image references on write."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if value is None:
            return ""
        return str(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if not value:
            return ""
        return str(value).strip().lower()

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        """Dict -> string for form display."""
        if isinstance(value, dict):
            return value.get("reference", "")
        if isinstance(value, str):
            return value
        return ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        """String -> dict for YAML storage."""
        if not value or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, dict):
            return value
        return {
            "type": "deployment",
            "reference": str(value),
            "mode": "once",
        }

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if not value or not isinstance(value, list):
            return "Geen deployment services"
        names = [s.get("reference", "onbekend") for s in value if isinstance(s, dict)]
        return ", ".join(names) if names else "Geen deployment services"


class KeycloakRealmsDisplayConverter:
    """Formats keycloak realm list for display."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> list[dict[str, str]]:
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return self.view(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value  # passthrough - merge happens in post-processing

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
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

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
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

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """Masked display - never show encrypted content in UI."""
        if not value:
            return "Niet geconfigureerd"
        if isinstance(value, str) and "BEGIN AGE ENCRYPTED FILE" in value:
            return "********"
        return "********"


# ---------------------------------------------------------------------------
# RRULE schedule converters
# ---------------------------------------------------------------------------


def _get_schedule_from_context(context_data: dict[str, Any] | None, path_hint: str) -> str:
    """Extract the schedule RRULE value from context_data using path proximity.

    The transient fields live at paths like ``deployments[0]/backup/schedule:time``.
    The parent RRULE is at ``deployments[0]/backup/schedule``.
    """
    if not context_data:
        return ""

    # Extract the parent path by removing the :suffix
    parent_path = re.sub(r":[^/]+$", "", path_hint)
    value = smart_get_value(context_data, parent_path)
    return str(value) if value else ""


class RRuleFrequencyConverter:
    """Reads/writes the FREQ part of an RRULE schedule string.

    This is the main schedule field converter. On write, it combines
    the frequency with transient time/day fields from context_data
    to produce the full RRULE string.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """Extract frequency from RRULE for the select dropdown."""
        if not value:
            return ""
        parts = parse_rrule(str(value))
        return parts.get("FREQ", "").upper()

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None:
        """Combine frequency + transient fields into RRULE string."""
        freq = str(value).strip().upper() if value else ""
        if not freq:
            return None

        # Read transient field values from context_data (already written by processor)
        hour = 2
        minute = 0
        byday = ""
        bymonthday = ""

        if context_data:
            # Find the transient values — they're written to context under their paths
            # We need to search for any deployment index; scan deployments list
            deployments = context_data.get("deployments", [])
            for _i, dep in enumerate(deployments if isinstance(deployments, list) else []):
                backup = dep.get("backup", {}) if isinstance(dep, dict) else {}
                if not isinstance(backup, dict):
                    continue
                time_val = backup.get("schedule:time", "")
                day_val = backup.get("schedule:day", "")
                monthday_val = backup.get("schedule:monthday", "")
                if time_val or day_val or monthday_val:
                    if time_val and ":" in str(time_val):
                        h, _, m = str(time_val).partition(":")
                        hour = int(h) if h.isdigit() else 2
                        minute = int(m) if m.isdigit() else 0
                    byday = str(day_val) if day_val else ""
                    bymonthday = str(monthday_val) if monthday_val else ""
                    break

        return build_rrule(freq, hour, minute, byday, bymonthday)

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """Human-readable schedule summary."""
        return format_rrule(str(value) if value else None)


def _find_parent_rrule(context_data: dict[str, Any] | None) -> str:
    """Find the RRULE string from the first deployment with a schedule."""
    if not context_data:
        return ""
    deployments = context_data.get("deployments", [])
    for dep in deployments if isinstance(deployments, list) else []:
        if not isinstance(dep, dict):
            continue
        backup = dep.get("backup", {})
        if isinstance(backup, dict):
            schedule = backup.get("schedule", "")
            if schedule and "FREQ=" in str(schedule):
                return str(schedule)
    return ""


class RRuleTimeConverter:
    """Transient field converter: extracts/ignores the time portion of an RRULE.

    Reads BYHOUR and BYMINUTE from the parent schedule in context_data.
    Write is a no-op (RRuleFrequencyConverter combines all fields).
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """Extract time from parent RRULE in context_data."""
        # value is None for transient fields; read from context instead
        rrule = _find_parent_rrule(context_data)
        if not rrule:
            return "02:00"  # default
        parts = parse_rrule(rrule)
        hour = parts.get("BYHOUR", "2")
        minute = parts.get("BYMINUTE", "0")
        return f"{int(hour):02d}:{int(minute):02d}"

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        """Pass through — RRuleFrequencyConverter reads this from context."""
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value, context_data=context_data)


class RRuleDayConverter:
    """Transient field converter: extracts BYDAY from the parent RRULE."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        rrule = _find_parent_rrule(context_data)
        if not rrule:
            return "MO"
        parts = parse_rrule(rrule)
        return parts.get("BYDAY", "MO")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value, context_data=context_data)


class RRuleMonthDayConverter:
    """Transient field converter: extracts BYMONTHDAY from the parent RRULE."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        rrule = _find_parent_rrule(context_data)
        if not rrule:
            return "1"
        parts = parse_rrule(rrule)
        return parts.get("BYMONTHDAY", "1")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value, context_data=context_data)
