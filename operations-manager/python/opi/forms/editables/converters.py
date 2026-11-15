from __future__ import annotations

import logging
from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.core.rrule_utils import build_rrule, format_rrule, parse_rrule
from opi.utils.age import (
    AgeEncryptionError,
    carries_encrypted_value,
    encrypt_age_content_sync,
    get_project_public_key,
    is_age_encrypted,
    project_private_key_or_none,
)
from opi.utils.url_paths import normalize_logout_path
from opi.utils.yaml_util import dump_yaml_to_string

logger = logging.getLogger(__name__)


def keep_existing_ciphertext_if_unchanged(existing: Any, new: Any, context_data: dict[str, Any] | None) -> Any:  # noqa: ANN401  # a project-file value
    """Return *existing* when both values are AGE blocks with identical plaintext.

    AGE encryption is non-deterministic, so re-encrypting unchanged plaintext yields entirely
    different ciphertext and every form save would rewrite the block in git. On any doubt (no
    key, decrypt failure) *new* is returned, so changed content or a rotated key still
    re-encrypts.
    """
    if not (isinstance(existing, str) and isinstance(new, str)) or existing == new:
        return new
    if not (is_age_encrypted(existing) and is_age_encrypted(new)):
        return new

    private_key = project_private_key_or_none(context_data)
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
    """Shows an encrypted field as a status, never as a value. The field is read-only."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if carries_encrypted_value(value):
            return "Versleuteld opgeslagen"
        if value:
            return "Geconfigureerd"
        return "Niet geconfigureerd"


class TruncateConverter:
    def __init__(self, max_length: int = 20) -> None:
        self.max_length = max_length

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value or ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        if not value:
            return None
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return str(value) if value else ""


class LogoutPathConverter:
    """Writes the logout path normalized, so the form and the API produce the same file.

    The API normalizes through the ``field_validator`` of ``KeycloakConfig``, so without this
    converter the same input would yield a different project file per entrance.

    An invalid value comes out unchanged. Validating and writing are decoupled in the processor
    (``_write_field`` also runs when ``_validate_field`` found errors), so raising here would
    break the save instead of showing the message of ``LogoutPathValidator`` at the field.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value or ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        if not value:
            return None
        try:
            return normalize_logout_path(value)
        except ValueError:
            return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return str(value) if value else ""


class EnsureListConverter:
    """Coerces any scalar or None value to a list.

    Use on any editable whose YAML value is always a list but whose form
    transport may deliver a single string (e.g. HTMX checkbox_group with
    one item checked) or None (no items checked).
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[Any]:  # noqa: ANN401  # a project-file value
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value


class ServiceListConverter:
    """Converts mixed str/dict service list to/from structured format.

    ``preserve_catalog_data`` is for the PROJECT services list only. A component references
    attachments through its ``config``, so restoring the catalog ``data`` onto a component's
    services list would copy the whole catalog onto the component.
    """

    def __init__(self, preserve_catalog_data: bool = False) -> None:
        self._preserve_catalog_data = preserve_catalog_data

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:  # noqa: ANN401  # a project-file value
        from opi.services.services import ServiceAdapter

        if not value or not isinstance(value, list):
            return []
        return ServiceAdapter.extract_service_names_from_project_services(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str | dict]:  # noqa: ANN401  # a project-file value
        """Convert service names back to a simple list; configs are added separately.

        ``json-enc`` delivers a single checked box as a scalar instead of an array, and it
        promotes a storage entry to a dict (``{"persistent-storage": {"config": ...}}``); such
        a dict is kept as it is.

        The attachments catalog ``data`` is managed by the project Bijlagen UI, so it is absent
        from this submission and is restored from the existing project data onto the
        still-selected ``attachments`` entry. Without that, saving the services list silently
        drops every uploaded attachment.
        """
        from opi.forms.wizard.services_merge import dedupe_service_list

        if isinstance(value, str) and value:
            value = [value]
        if not isinstance(value, list):
            return []

        # In the project file only the first entry of a name is promoted to a config record,
        # so a duplicate is collapsed here instead of reaching it.
        value = dedupe_service_list(value)

        if self._preserve_catalog_data:
            existing_data = self._existing_attachments_data(context_data)
            if existing_data is not None:
                value = [self._restore_attachments_data(item, existing_data) for item in value]
        return value

    @staticmethod
    def _existing_attachments_data(context_data: dict[str, Any] | None) -> Any:  # noqa: ANN401  # a project-file value
        for entry in (context_data or {}).get("services", []):
            if isinstance(entry, dict) and isinstance(entry.get("attachments"), dict):
                return entry["attachments"].get("data")
        return None

    @staticmethod
    def _restore_attachments_data(item: Any, existing_data: Any) -> Any:  # noqa: ANN401  # a project-file value
        from opi.services.services import service_entry_name

        name = service_entry_name(item)
        if name != "attachments":
            return item
        current = item["attachments"] if isinstance(item, dict) and isinstance(item.get("attachments"), dict) else {}
        return {"attachments": {**current, "data": existing_data}}

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:  # noqa: ANN401  # a project-file value
        return self.read(value)


class IntegerConverter:
    """Converts a single integer to/from string for text input."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if value is None:
            return ""
        return str(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> int | None:  # noqa: ANN401  # a project-file value
        if isinstance(value, int):
            return value
        val = str(value).strip()
        if val.isdigit():
            return int(val)
        return None

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value)


class IntegerListConverter:
    """Converts list[int] to/from comma-separated string."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[int]:  # noqa: ANN401  # a project-file value
        if isinstance(value, list):
            return [int(v) for v in value if str(v).strip().isdigit()]
        return [int(v.strip()) for v in str(value).split(",") if v.strip().isdigit()]

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value)


class ListSingleSelectConverter:
    """Converts a YAML list to/from a single select value.

    For an editable whose YAML holds a list (``clusters: [local]``) while the form shows a
    single-select dropdown.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if isinstance(value, list) and value:
            return str(value[0])
        return ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:  # noqa: ANN401  # a project-file value
        if isinstance(value, list):
            return value
        if value:
            return [str(value)]
        return []

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value)


class KeyValueConverter:
    """Converts KEY=value text to/from YAML storage format.

    ``write_as`` picks the storage shape: ``"dict"`` parses the text into a map and encrypts
    each value on its own, ``"string"`` keeps the raw text as one string literal (what
    ``user-env-vars`` and ``aliases`` use).

    An AGE-encrypted stored value is detected and decrypted by ``read()`` and ``view()`` with
    the project's private key from ``context_data``.
    """

    def __init__(self, fmt: str = "env", write_as: str = "dict") -> None:
        self.fmt = fmt  # "env" or "yaml"
        self.write_as = write_as  # "dict" or "string"

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        """Return the stored text for the editor, decrypted when it is an AGE block."""
        # Never log the value itself: user-env-vars and aliases hold secrets.
        logger.debug("[KeyValueConverter.read] write_as=%s, input type=%s", self.write_as, type(value).__name__)
        value = self._maybe_decrypt(value, context_data)
        if isinstance(value, dict):
            if not value:
                return ""
            if self.fmt == "env":
                return "\n".join(f"{k}={v}" for k, v in value.items())
            return dump_yaml_to_string(dict(value)).rstrip("\n")
        return str(value or "")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> dict[str, str] | str | None:  # noqa: ANN401  # a project-file value
        """Convert form input to the YAML storage format; empty input gives None.

        None lets the YAML key be omitted. With ``write_as="string"`` and a project AGE public
        key in *context_data* the result is encrypted.
        """
        logger.debug("[KeyValueConverter.write] write_as=%s, input type=%s", self.write_as, type(value).__name__)
        result = self._write_as_string(value) if self.write_as == "string" else self._write_as_dict(value)
        if result and self.write_as == "string" and isinstance(result, str):
            result = self._encrypt(result, context_data)
        elif result and self.write_as == "dict" and isinstance(result, dict):
            # A value in the map may hold a secret, so each one is encrypted on its own.
            result = {k: self._encrypt(str(v), context_data) for k, v in result.items()}
        logger.debug(
            "[KeyValueConverter.write] result type=%s", type(result).__name__ if result is not None else "None"
        )
        return result

    def _write_as_dict(self, value: Any) -> dict[str, str] | None:  # noqa: ANN401  # a project-file value
        if isinstance(value, dict):
            return value or None
        text = str(value or "").strip()
        if not text:
            return None
        return self._parse_env_text(text)

    @staticmethod
    def _write_as_string(value: Any) -> str | None:  # noqa: ANN401  # a project-file value
        if isinstance(value, dict):
            if not value:
                return None
            return "\n".join(f"{k}={v}" for k, v in value.items())
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _parse_env_text(text: str) -> dict[str, str]:
        """Parse KEY=value or YAML key-value text into a dict.

        Goes through ``validate_and_parse_env_vars`` so an alias written in YAML form
        (``KEY: "value with = inside"``) is not corrupted by a naive split on ``=``.
        """
        from opi.utils.env_vars import validate_and_parse_env_vars

        try:
            return validate_and_parse_env_vars(text)
        except ValueError, TypeError:
            # Validation upstream has already reported this, and a converter must not raise:
            # a permissive split keeps the save on the field error instead of a 500.
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
    def _maybe_decrypt(value: Any, context_data: dict[str, Any] | None) -> Any:  # noqa: ANN401  # a project-file value
        """Decrypt an AGE-encrypted value with the project's private key.

        Aliases are a dict of ``name -> value`` whose values are encrypted on their own, so
        a dict is decrypted per entry.
        """
        if isinstance(value, dict):
            return {k: KeyValueConverter._maybe_decrypt(v, context_data) for k, v in value.items()}
        if not isinstance(value, str) or not is_age_encrypted(value):
            return value
        if not context_data:
            return value
        try:
            from opi.utils.age import decrypt_age_content_sync

            project_private_key = project_private_key_or_none(context_data)
            if not project_private_key:
                logger.warning("[KeyValueConverter] Failed to resolve project private key")
                return value
            decrypted = decrypt_age_content_sync(value, project_private_key)
            if decrypted is not None:
                logger.debug("[KeyValueConverter] Successfully decrypted AGE-encrypted value")
                return decrypted
            return value
        except AgeEncryptionError, OSError, ValueError:
            logger.warning("[KeyValueConverter] AGE decryption failed, returning raw value", exc_info=True)
            return value

    @staticmethod
    def _encrypt(value: str, context_data: dict[str, Any] | None) -> str:
        """Encrypt a plain-text value with the project's AGE public key.

        Raises ``AgeEncryptionError`` when the project has no public key: the fields on this
        converter hold secrets, so returning the plain value would write it down.
        """
        if carries_encrypted_value(value):
            return value
        public_key = get_project_public_key(context_data or {})
        if not public_key:
            raise AgeEncryptionError(
                "Deze waarde kan niet worden opgeslagen: het project heeft geen AGE-publieke sleutel."
            )
        return LiteralScalarString(encrypt_age_content_sync(value, public_key))

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value, context_data=context_data)


class ContainerImageConverter:
    """Lowercases container image references on write."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if value is None:
            return ""
        return str(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None:  # noqa: ANN401  # a project-file value
        if not value:
            return None
        return str(value).strip().lower()

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        if isinstance(value, dict):
            return value.get("reference", "")
        if isinstance(value, str):
            return value
        return ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        if not value or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, dict):
            return value
        return {
            "type": "deployment",
            "reference": str(value),
            "mode": "once",
        }

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if not value or not isinstance(value, list):
            return "Geen deployment services"
        names = [s.get("reference", "onbekend") for s in value if isinstance(s, dict)]
        return ", ".join(names) if names else "Geen deployment services"


class KeycloakRealmsDisplayConverter:
    """Formats keycloak realm list for display."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> list[dict[str, str]]:  # noqa: ANN401  # a project-file value
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return self.view(value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value  # the merge happens in post-processing

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        if not value:
            return value
        from opi.core.config import settings
        from opi.services.catalog.publish_on_web.subdomain import get_supported_base_domains

        if str(value) not in get_supported_base_domains(cluster=settings.CLUSTER_MANAGER):
            return "__custom__"
        return value


class RRuleFrequencyConverter:
    """Reads and writes the FREQ part of an RRULE schedule string.

    On write it combines the frequency with the transient time and day fields from
    ``context_data`` into the full RRULE.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if not value:
            return ""
        parts = parse_rrule(str(value))
        return parts.get("FREQ", "").upper()

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None:  # noqa: ANN401  # a project-file value
        freq = str(value).strip().upper() if value else ""
        if not freq:
            return None

        # The transient fields are read from context_data, where the processor has already
        # written them; they live under their own path, so the deployments are scanned for one.
        hour = 2
        minute = 0
        byday = ""
        bymonthday = ""

        if context_data:
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

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return format_rrule(str(value) if value else None)


def _find_parent_rrule(context_data: dict[str, Any] | None) -> str:
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

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        # A transient field has no value of its own, so the time comes from the context.
        rrule = _find_parent_rrule(context_data)
        if not rrule:
            return "02:00"
        parts = parse_rrule(rrule)
        hour = parts.get("BYHOUR", "2")
        minute = parts.get("BYMINUTE", "0")
        return f"{int(hour):02d}:{int(minute):02d}"

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        """A no-op: ``RRuleFrequencyConverter`` picks this field up from the context."""
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value, context_data=context_data)


class RRuleDayConverter:
    """Transient field converter: extracts BYDAY from the parent RRULE."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        rrule = _find_parent_rrule(context_data)
        if not rrule:
            return "MO"
        parts = parse_rrule(rrule)
        return parts.get("BYDAY", "MO")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value, context_data=context_data)


class RRuleMonthDayConverter:
    """Transient field converter: extracts BYMONTHDAY from the parent RRULE."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        rrule = _find_parent_rrule(context_data)
        if not rrule:
            return "1"
        parts = parse_rrule(rrule)
        return parts.get("BYMONTHDAY", "1")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value, context_data=context_data)


class BooleanConverter:
    """Maps a Ja/Nee select (values "true"/"false") to a real YAML boolean.

    Use on boolean config fields whose cluster-wide default may be True: a form value
    that merely omits the key would inherit that default, so this always writes an
    explicit ``true``/``false`` instead.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if value is None or value == "":
            return ""
        return "true" if value in (True, "true", "on", "yes", "1") else "false"

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> bool:  # noqa: ANN401  # a project-file value
        return value in (True, "true", "on", "yes", "1")

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return "Ja" if value in (True, "true", "on", "yes", "1") else "Nee"


class CommaSeparatedListConverter:
    """Converts a list to/from a comma-separated string (trimmed, empties dropped)."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value or "")

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str]:  # noqa: ANN401  # a project-file value
        if isinstance(value, list):
            return value
        return [part.strip() for part in str(value).split(",") if part.strip()]

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return self.read(value, context_data=context_data)


class CommandLineConverter:
    """A start command as one line of text, stored as the list of arguments the cluster wants.

        sh -c "/app/docker/prod/seeder && exec /app/docker/prod/web"

    becomes

        ["sh", "-c", "/app/docker/prod/seeder && exec /app/docker/prod/web"]

    The splitting rules:

    * whitespace separates arguments;
    * what sits between double quotes stays together, spaces included;
    * a double quote is a separator and does not reach the container. A literal one is
      written doubled (``""``), as in a spreadsheet.

    Unlike a shell there are no backslash escapes: a backslash reaches the container as it
    was typed.
    """

    QUOTE = '"'

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if not isinstance(value, list):
            return "" if value is None else str(value)
        return " ".join(self._quote(str(argument)) for argument in value)

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[str] | None:  # noqa: ANN401  # a project-file value
        """The typed line as the list the cluster gets; empty gives None.

        None lets ``remove_when_none`` drop the key. The schema demands at least one
        argument, so an empty list would make the project invalid.
        """
        if isinstance(value, list):
            arguments = [str(item) for item in value if str(item).strip()]
            return arguments or None
        arguments = split_command_line(str(value or ""))
        return arguments or None

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if not value:
            return "Zoals in het image"
        return self.read(value, context_data)

    def _quote(self, argument: str) -> str:
        doubled = argument.replace(self.QUOTE, self.QUOTE * 2)
        if argument == "" or any(character.isspace() for character in argument) or self.QUOTE in argument:
            return f"{self.QUOTE}{doubled}{self.QUOTE}"
        return doubled


def split_command_line(line: str) -> list[str]:
    """Split a command line into arguments; ``CommandLineConverter`` holds the rules.

    An unterminated quote is closed here as if it stood at the end. The validator is what
    complains about it, so the user gets a message instead of a silently different command.
    """
    arguments: list[str] = []
    current: list[str] = []
    started = False
    in_quote = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == CommandLineConverter.QUOTE:
            if line[index + 1 : index + 2] == CommandLineConverter.QUOTE:
                current.append(CommandLineConverter.QUOTE)
                started = True
                index += 2
                continue
            in_quote = not in_quote
            started = True
            index += 1
            continue
        if character.isspace() and not in_quote:
            if started:
                arguments.append("".join(current))
                current, started = [], False
            index += 1
            continue
        current.append(character)
        started = True
        index += 1
    if started:
        arguments.append("".join(current))
    return arguments


def command_line_has_unbalanced_quote(line: str) -> bool:
    """Whether a double quote is still open at the end of the line."""
    open_quote = False
    index = 0
    while index < len(line):
        if line[index] == CommandLineConverter.QUOTE:
            if line[index + 1 : index + 2] == CommandLineConverter.QUOTE:
                index += 2
                continue
            open_quote = not open_quote
        index += 1
    return open_quote


class NonEmptyListConverter:
    """Keeps an empty choice out of the list.

    For a sequence of fixed size whose empty option means "none". Such a sequence always
    renders a row, so without this "assign no role" stores a list with an empty string,
    which is a role without a name rather than no role.

    Returns None when nothing is left, so ``remove_when_none`` drops the key instead of
    writing an empty list.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> list[Any] | None:  # noqa: ANN401  # a project-file value
        if value is None:
            return None
        items = value if isinstance(value, list) else [value]
        remaining = [item for item in items if item is not None and str(item).strip() != ""]
        return remaining or None

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return value


class InviteTargetConverter:
    """Address (form) <-> ``component:deployment[:/path]`` (project file).

    The destination picker shows addresses, which a human recognizes, and stores the choice,
    which survives a change of subdomain, domain format or cluster. The derivation itself
    lives in ``opi/services/catalog/invite/destination.py``, shared with the public invite
    page; that page is not a form and has its own entrance (``resolve_invite_url``).

    An address that is not of this project yields no choice and nothing is written. That
    covers both the empty option ("show no button") and an option that no longer resolves:
    for the file those mean the same, that no choice is fixed here. A fixed
    ``application-url`` to a destination outside the project is a field of its own.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        """The stored choice as the address the picker should preselect.

        A choice that no longer resolves selects nothing, just as the success page shows no
        button for it.
        """
        from opi.services.catalog.invite.destination import url_for_target

        if not value or not isinstance(value, str) or not context_data:
            return ""
        return url_for_target(value, context_data) or ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None:  # noqa: ANN401  # a project-file value
        """The chosen address as the choice behind it, or None to write nothing.

        A value that already is a choice passes unchanged: the whole-flow validation feeds
        the merged project file back in as the submission.
        """
        from opi.services.catalog.invite.destination import target_for_url
        from opi.services.catalog.invite.target_format import split_target

        if not value or not isinstance(value, str) or not context_data:
            return None
        if split_target(value)[0]:
            return value
        return target_for_url(value, context_data)

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        """Read-only view: the address this points at today."""
        from opi.services.catalog.invite.destination import url_for_target

        if not value or not isinstance(value, str):
            return "Geen knop tonen"
        if not context_data:
            return value
        return url_for_target(value, context_data) or "Bestemming bestaat niet meer"
