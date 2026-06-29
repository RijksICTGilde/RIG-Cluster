from __future__ import annotations

import re
from typing import Any


class SlugValidator:
    """
    Validates slug format: starts with letter, only lowercase letters, digits, hyphens.

    Pattern: ^[a-z][a-z0-9-]*$
    """

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # Let RequiredValidator handle emptiness
        value_str = str(value)
        if not re.match(r"^[a-z][a-z0-9-]*$", value_str):
            return ["Moet beginnen met een kleine letter en mag alleen kleine letters, cijfers en streepjes bevatten"]
        return []


class EmailValidator:
    """Validates basic email format."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # Let RequiredValidator handle emptiness
        value_str = str(value)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value_str):
            return ["Geen geldig e-mailadres"]
        return []


class MinMaxLengthValidator:
    """Validates minimum and/or maximum string length."""

    def __init__(
        self,
        min_length: int | None = None,
        max_length: int | None = None,
    ) -> None:
        self.min_length = min_length
        self.max_length = max_length

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # Let RequiredValidator handle emptiness
        value_str = str(value)
        errors: list[str] = []
        if self.min_length is not None and len(value_str) < self.min_length:
            errors.append(f"Moet minimaal {self.min_length} tekens bevatten")
        if self.max_length is not None and len(value_str) > self.max_length:
            errors.append(f"Mag maximaal {self.max_length} tekens bevatten")
        return errors


class RangeValidator:
    """Validates that a numeric value falls within a specified range."""

    def __init__(
        self,
        min_value: int | float | None = None,
        max_value: int | float | None = None,
    ) -> None:
        self.min_value = min_value
        self.max_value = max_value

    def validate(self, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        try:
            num = float(value)
        except ValueError, TypeError:
            return ["Moet een geldig getal zijn"]
        errors: list[str] = []
        if self.min_value is not None and num < self.min_value:
            errors.append(f"Moet minimaal {self.min_value} zijn")
        if self.max_value is not None and num > self.max_value:
            errors.append(f"Mag maximaal {self.max_value} zijn")
        return errors


class ComponentNameValidator:
    """
    Validates component names: lowercase letters and digits only, max 12 chars.

    Pattern: ^[a-z][a-z0-9]{0,11}$

    When called with context containing ``existing_component_names``,
    also checks uniqueness.
    """

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 12:
            return ["Componentnaam mag maximaal 12 tekens bevatten"]
        if not re.match(r"^[a-z][a-z0-9]*$", value_str):
            return ["Moet beginnen met een kleine letter en mag alleen kleine letters en cijfers bevatten"]
        if context and value_str in context.get("existing_component_names", []):
            return [f"Er bestaat al een component met de naam '{value_str}'"]
        return []


class DeploymentNameValidator:
    """Validates a deployment name as a Kubernetes-safe label.

    The name becomes part of Kubernetes resource names, so it must start with a lowercase
    letter (also keeps it out of YAML's int parsing, so never all-digits) and then contain
    only lowercase letters, digits and hyphens, ending alphanumeric, max 63 chars. The
    explicit message names the common mistakes (spaces, capitals) since the wizard field is
    free text. Cross-deployment uniqueness is handled by UniqueDeploymentNameEnforcer.
    """

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 63:
            return ["Deploymentnaam mag maximaal 63 tekens bevatten"]
        if not re.match(r"^[a-z]([-a-z0-9]*[a-z0-9])?$", value_str):
            return [
                "Deploymentnaam moet met een kleine letter beginnen en mag alleen kleine letters, "
                "cijfers en streepjes bevatten, geen spaties of hoofdletters"
            ]
        return []


class AttachmentIdValidator:
    """
    Validates attachment ids: lowercase letters, digits and hyphens, starting with a
    letter and ending alphanumeric, max 40 chars. The id becomes part of a Kubernetes
    volume name (``attch-{id}``), a DNS-1123 label capped at 63 chars, so 40 leaves
    margin while allowing descriptive names.

    When called with context containing ``existing_attachment_ids``, also checks uniqueness.
    """

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 40:
            return ["Bijlage-id mag maximaal 40 tekens bevatten"]
        if not re.match(r"^[a-z]([a-z0-9-]*[a-z0-9])?$", value_str):
            return [
                "Moet beginnen met een kleine letter, mag kleine letters, cijfers en streepjes bevatten, "
                "en moet eindigen op een letter of cijfer"
            ]
        if context and value_str in context.get("existing_attachment_ids", []):
            return [f"Er bestaat al een bijlage met de id '{value_str}'"]
        return []


class ContainerImageValidator:
    """Validates container image references.

    Enforces lowercase, no spaces, and the same allowed-character set as
    the project schema's ``image`` pattern (alphanumerics plus ``._:/@-``,
    starting with an alphanumeric). Keeping this in sync with the schema
    means illegal characters are rejected up front with a clear message
    instead of failing late during ``validate_project_schema``.
    """

    # Mirrors the "image" pattern in opi/schemas/project_v2.json.
    _PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._:/@-]*)?\Z")
    _ALLOWED_CHAR = re.compile(r"[A-Za-z0-9._:/@-]")

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        errors: list[str] = []
        if value_str != value_str.lower():
            errors.append("Container image moet volledig in kleine letters zijn")
        if " " in value_str:
            errors.append("Container image mag geen spaties bevatten")
        if not self._PATTERN.match(value_str):
            # Report the offending characters (spaces are flagged separately).
            invalid = sorted({c for c in value_str if c != " " and not self._ALLOWED_CHAR.match(c)})
            if invalid:
                errors.append("Container image bevat niet-toegestane tekens: " + " ".join(invalid))
            elif not errors:
                errors.append("Container image moet beginnen met een letter of cijfer")
        return errors


class RealmRoleValidator:
    """Validates Keycloak realm role names: alphanumeric, hyphens, underscores, max 255 chars."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 255:
            return ["Rolnaam mag maximaal 255 tekens bevatten"]
        if not re.match(r"^[a-zA-Z0-9_-]+$", value_str):
            return ["Rolnaam mag alleen letters, cijfers, streepjes en underscores bevatten"]
        return []


class PathValidator:
    """Validates publication path format: must start with / and contain no spaces."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        v = str(value)
        if not v.startswith("/"):
            return ["Pad moet beginnen met /"]
        if " " in v:
            return ["Pad mag geen spaties bevatten"]
        return []


class UrlValidator:
    """Validates that a value is a valid HTTP(S) URL."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        v = str(value)
        if not v.startswith("http://") and not v.startswith("https://"):
            return ["Moet beginnen met http:// of https://"]
        return []


class RequiredValidator:
    """Validates that a field has a non-empty value."""

    def validate(self, value: Any) -> list[str]:
        if value is None:
            return ["Dit veld is verplicht"]
        if isinstance(value, str) and not value.strip():
            return ["Dit veld is verplicht"]
        if isinstance(value, list) and len(value) == 0:
            return ["Dit veld is verplicht"]
        return []


class KeyValueValidator:
    """Validates that text is parseable as ENV (KEY=value) or YAML key-value pairs.

    Delegates to ``validate_and_parse_env_vars`` which is the same parser
    used at deploy time, so validation here matches what will actually be
    accepted.
    """

    def validate(self, value: Any) -> list[str]:
        if not value or (isinstance(value, str) and not value.strip()):
            return []
        if not isinstance(value, str):
            return []
        try:
            from opi.utils.env_vars import validate_and_parse_env_vars

            validate_and_parse_env_vars(value)
        except (ValueError, TypeError) as e:
            return [str(e)]
        return []


class AllowedValuesValidator:
    """Validates that a value is one of the allowed options."""

    def __init__(self, allowed: list[str]) -> None:
        self.allowed = allowed

    def validate(self, value: Any) -> list[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return []
        if str(value) not in self.allowed:
            return [f"Ongeldige waarde: {value}. Toegestaan: {', '.join(self.allowed)}"]
        return []


class MemoryRangeValidator:
    """Validates that a K8s memory string falls within a min/max range.

    Parses values like ``256Mi``, ``1Gi``, ``384Mi`` and checks that the
    value in MiB is between *min_mi* and *max_mi* (inclusive).

    When *max_mi* is None the cluster's ``max_memory_limit_mi`` is used.
    """

    def __init__(self, min_mi: int = 25, max_mi: int | None = None) -> None:
        self.min_mi = min_mi
        self._max_mi = max_mi

    @property
    def max_mi(self) -> int:
        if self._max_mi is not None:
            return self._max_mi
        from opi.core.cluster_config import get_max_memory_limit_mi
        from opi.core.config import settings

        return get_max_memory_limit_mi(settings.CLUSTER_MANAGER)

    def validate(self, value: Any) -> list[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return []
        from opi.services.resource_analyzer import parse_k8s_memory_to_mi

        try:
            mi = parse_k8s_memory_to_mi(str(value))
        except ValueError:
            return [f"Ongeldige geheugenwaarde: {value}"]
        max_mi = self.max_mi
        if mi < self.min_mi or mi > max_mi:
            return [f"Geheugen moet tussen {self.min_mi}Mi en {max_mi}Mi liggen (was: {value})"]
        return []


class MemoryRequestRangeValidator(MemoryRangeValidator):
    """Validates memory requests against the lower request cap."""

    @property
    def max_mi(self) -> int:
        if self._max_mi is not None:
            return self._max_mi
        from opi.core.cluster_config import get_max_memory_request_mi
        from opi.core.config import settings

        return get_max_memory_request_mi(settings.CLUSTER_MANAGER)


class SubdomainValidator:
    """Validates subdomain format using the canonical validation from subdomain connector."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        from opi.connectors.subdomain import validate_subdomain

        is_valid, error_msg = validate_subdomain(str(value))
        return [error_msg] if not is_valid and error_msg else []


class BaseDomainValidator:
    """Validates that a base domain is in the supported domains list."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        if str(value) == "__custom__":
            return []  # Sentinel; actual domain validated via CustomDomainValidator
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error_msg = validate_base_domain(str(value))
        return [error_msg] if not is_valid and error_msg else []


class CustomDomainValidator:
    """Validates custom domain format."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        domain = str(value).strip().lower()
        if not re.match(
            r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$",
            domain,
        ):
            return ["Ongeldig domeinformaat. Gebruik een geldig domeinnaam zoals 'voorbeeld.nl'"]
        return []


class DomainFormatValidator:
    """Validates that domain-format is a known template ID."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES

        if str(value) not in DOMAIN_FORMAT_TEMPLATES:
            return [f"Onbekend URL-formaat: {value}"]
        return []
