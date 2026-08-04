from __future__ import annotations

import logging
from typing import Any

import yaml

from opi.core.rrule_utils import build_rrule, format_rrule, parse_rrule

logger = logging.getLogger(__name__)


def resolve_project_private_key(context_data: dict[str, Any] | None) -> str | None:
    """Resolve the project's AGE private key from form context data.

    The project file stores the project private key encrypted with the
    system key (``config.age-private-key``); decrypt it so field values
    encrypted with the project key can be read.

    Returns None when the key cannot be resolved.
    """
    if not context_data:
        return None
    from opi.core.config import settings
    from opi.utils.age import decrypt_age_content_sync

    system_private_key = settings.SOPS_AGE_PRIVATE_KEY
    if not system_private_key:
        logger.warning("[resolve_project_private_key] No system AGE private key available")
        return None
    encoded_project_key = context_data.get("config", {}).get("age-private-key")
    if not encoded_project_key:
        logger.warning("[resolve_project_private_key] No project age-private-key in context_data")
        return None
    return decrypt_age_content_sync(encoded_project_key, system_private_key)


def keep_existing_ciphertext_if_unchanged(existing: Any, new: Any, context_data: dict[str, Any] | None) -> Any:
    """Return *existing* when both values are AGE blocks with identical plaintext.

    AGE encryption is non-deterministic: re-encrypting unchanged plaintext
    produces entirely different ciphertext, so every form save would rewrite
    the block in git (pure-churn diffs and push conflicts with concurrent
    commits). When the freshly encrypted value decrypts to the same plaintext
    as the stored one, keep the stored ciphertext verbatim. On any doubt
    (missing key, decrypt failure) return *new*, so changed content or a
    rotated key still forces re-encryption.
    """
    if not (isinstance(existing, str) and isinstance(new, str)) or existing == new:
        return new
    if "BEGIN AGE ENCRYPTED FILE" not in existing or "BEGIN AGE ENCRYPTED FILE" not in new:
        return new

    private_key = resolve_project_private_key(context_data)
    if not private_key:
        return new

    from opi.utils.age import decrypt_age_content_sync

    existing_plain = decrypt_age_content_sync(existing, private_key)
    new_plain = decrypt_age_content_sync(new, private_key)
    if existing_plain is not None and existing_plain == new_plain:
        logger.debug("[keep_existing_ciphertext_if_unchanged] Plaintext unchanged, keeping stored ciphertext")
        return existing
    return new


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

    def __init__(self, preserve_catalog_data: bool = False) -> None:
        # The attachments catalog ``data`` lives only on the PROJECT services list. Restoring
        # it is correct there, but it must NOT be restored onto a component's services list (a
        # component only references attachments via ``config``); doing so duplicates the whole
        # catalog onto the component. Only the project-level editable opts in.
        self._preserve_catalog_data = preserve_catalog_data

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

        The attachments catalog ``data`` (uploaded files, encrypted) is managed by the
        project Bijlagen UI, not this service-selection form, so it is absent from the
        submission. It is restored from the existing project data onto the still-selected
        ``attachments`` entry; otherwise saving the services list would silently drop
        every uploaded attachment.
        """
        from opi.forms.wizard.services_merge import dedupe_service_list

        if isinstance(value, str) and value:
            value = [value]
        if not isinstance(value, list):
            return []

        # The picker can post the same service twice: a locked service renders both a
        # disabled checkbox and a hidden input, and unlocking it client-side re-enables
        # the checkbox while the hidden input stays behind. The selection is a set keyed
        # by service name, so collapse it here rather than letting the duplicate reach
        # the project file (where only the first entry gets promoted to a config record).
        value = dedupe_service_list(value)

        if self._preserve_catalog_data:
            existing_data = self._existing_attachments_data(context_data)
            if existing_data is not None:
                value = [self._restore_attachments_data(item, existing_data) for item in value]
        return value

    @staticmethod
    def _existing_attachments_data(context_data: dict[str, Any] | None) -> Any:
        for entry in (context_data or {}).get("services", []):
            if isinstance(entry, dict) and isinstance(entry.get("attachments"), dict):
                return entry["attachments"].get("data")
        return None

    @staticmethod
    def _restore_attachments_data(item: Any, existing_data: Any) -> Any:
        from opi.services.services import service_entry_name

        name = service_entry_name(item)
        if name != "attachments":
            return item
        current = item["attachments"] if isinstance(item, dict) and isinstance(item.get("attachments"), dict) else {}
        return {"attachments": {**current, "data": existing_data}}

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
        # Never log the value: this converter carries user-env-vars and aliases, which
        # hold secrets (the deploy path holds the same rule, project_manager.py).
        logger.debug("[KeyValueConverter.read] write_as=%s, input type=%s", self.write_as, type(value).__name__)
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
        logger.debug("[KeyValueConverter.write] write_as=%s, input type=%s", self.write_as, type(value).__name__)
        result = self._write_as_string(value) if self.write_as == "string" else self._write_as_dict(value)
        if result and self.write_as == "string" and isinstance(result, str):
            result = self._maybe_encrypt(result, context_data)
        elif result and self.write_as == "dict" and isinstance(result, dict):
            # Aliases: encrypt each value independently (values may hold secrets).
            result = {k: self._maybe_encrypt(str(v), context_data) for k, v in result.items()}
        logger.debug(
            "[KeyValueConverter.write] result type=%s", type(result).__name__ if result is not None else "None"
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
        """Parse KEY=value or YAML key-value text into a dict.

        Delegates to ``validate_and_parse_env_vars`` so that aliases entered
        in YAML form (``KEY: "value with = inside"``) are not corrupted by
        a naive split on ``=``. Falls back to a permissive KEY=VALUE split
        if parsing fails, matching the previous behavior for malformed input.
        """
        from opi.utils.env_vars import validate_and_parse_env_vars

        try:
            return validate_and_parse_env_vars(text)
        except ValueError, TypeError:
            # Validation should have caught this upstream; preserve the old
            # permissive behavior so we never silently raise from the converter.
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
        """Decrypt AGE-encrypted value using the project's private key.

        Aliases are stored as a dict of ``name -> value``; each value may be
        AGE-encrypted independently, so decrypt them per-entry.
        """
        if isinstance(value, dict):
            return {k: KeyValueConverter._maybe_decrypt(v, context_data) for k, v in value.items()}
        if not isinstance(value, str) or "BEGIN AGE ENCRYPTED FILE" not in value:
            return value
        if not context_data:
            return value
        try:
            from opi.utils.age import decrypt_age_content_sync

            project_private_key = resolve_project_private_key(context_data)
            if not project_private_key:
                logger.warning("[KeyValueConverter] Failed to resolve project private key")
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
            logger.debug("[KeyValueConverter] Encrypted value with project AGE key")
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

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None:
        if not value:
            return None
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


class BooleanConverter:
    """Maps a Ja/Nee select (values "true"/"false") to a real YAML boolean.

    Use on boolean config fields whose cluster-wide default may be True: a form value
    that merely omits the key would inherit that default, so this always writes an
    explicit ``true``/``false`` instead.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if value is None or value == "":
            return ""
        return "true" if value in (True, "true", "on", "yes", "1") else "false"

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> bool:
        return value in (True, "true", "on", "yes", "1")

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return "Ja" if value in (True, "true", "on", "yes", "1") else "Nee"


class CommaSeparatedListConverter:
    """Converts a list to/from a comma-separated string (trimmed, empties dropped)."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:
        if isinstance(value, list):
            return value
        return [part.strip() for part in str(value).split(",") if part.strip()]

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return self.read(value, context_data=context_data)
