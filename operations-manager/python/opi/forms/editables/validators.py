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
