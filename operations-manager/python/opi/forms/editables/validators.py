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
        except (ValueError, TypeError):
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
    """

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 12:
            return ["Componentnaam mag maximaal 12 tekens bevatten"]
        if not re.match(r"^[a-z][a-z0-9]*$", value_str):
            return ["Moet beginnen met een kleine letter en mag alleen kleine letters en cijfers bevatten"]
        return []


class ContainerImageValidator:
    """Validates container image references are lowercase."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if value_str != value_str.lower():
            return ["Container image moet volledig in kleine letters zijn"]
        if " " in value_str:
            return ["Container image mag geen spaties bevatten"]
        return []


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
