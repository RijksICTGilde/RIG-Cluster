# Sub-part E: Validators

**Layer:** 1 (depends on Sub-part A for protocol definitions)
**Files to create:**
- `opi/forms/editables/validators.py`
- `tests/test_editables_validators.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

5 sync validators implementing the `EditableValidator` protocol from `opi/forms/editables/editable.py`.

```python
class EditableValidator(Protocol):
    def validate(self, value: Any) -> list[str]:
        """Return error messages (empty list = valid)."""
        ...
```

All error messages must be in **Dutch**.

---

## Validators

### SlugValidator

Validates lowercase slug format used for project names, component names, etc.

```python
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
            return [
                "Moet beginnen met een kleine letter en mag alleen kleine letters, "
                "cijfers en streepjes bevatten"
            ]
        return []
```

### EmailValidator

Simple email format check.

```python
class EmailValidator:
    """Validates basic email format."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # Let RequiredValidator handle emptiness
        value_str = str(value)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value_str):
            return ["Geen geldig e-mailadres"]
        return []
```

### MinMaxLengthValidator

Validates string length constraints.

```python
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
```

### RangeValidator

Validates numeric value is within a range.

```python
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
```

### RequiredValidator

Validates that a value is not empty.

```python
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
```

---

## Tests: test_editables_validators.py

```python
class TestSlugValidator:
    def test_valid_slug(self):
        assert SlugValidator().validate("my-project-1") == []

    def test_valid_single_letter(self):
        assert SlugValidator().validate("a") == []

    def test_starts_with_number(self):
        errors = SlugValidator().validate("1project")
        assert len(errors) == 1

    def test_contains_uppercase(self):
        errors = SlugValidator().validate("MyProject")
        assert len(errors) == 1

    def test_contains_special_chars(self):
        errors = SlugValidator().validate("my_project")
        assert len(errors) == 1

    def test_contains_spaces(self):
        errors = SlugValidator().validate("my project")
        assert len(errors) == 1

    def test_empty_returns_no_errors(self):
        """Empty values handled by RequiredValidator, not SlugValidator."""
        assert SlugValidator().validate("") == []
        assert SlugValidator().validate(None) == []


class TestEmailValidator:
    def test_valid_email(self):
        assert EmailValidator().validate("user@example.nl") == []

    def test_valid_email_with_subdomain(self):
        assert EmailValidator().validate("user@mail.example.nl") == []

    def test_missing_at(self):
        errors = EmailValidator().validate("userexample.nl")
        assert len(errors) == 1
        assert "e-mailadres" in errors[0]

    def test_missing_domain(self):
        errors = EmailValidator().validate("user@")
        assert len(errors) == 1

    def test_missing_tld(self):
        errors = EmailValidator().validate("user@example")
        assert len(errors) == 1

    def test_empty_returns_no_errors(self):
        assert EmailValidator().validate("") == []
        assert EmailValidator().validate(None) == []


class TestMinMaxLengthValidator:
    def test_within_range(self):
        validator = MinMaxLengthValidator(min_length=3, max_length=10)
        assert validator.validate("hello") == []

    def test_too_short(self):
        validator = MinMaxLengthValidator(min_length=5)
        errors = validator.validate("ab")
        assert len(errors) == 1
        assert "minimaal" in errors[0]

    def test_too_long(self):
        validator = MinMaxLengthValidator(max_length=3)
        errors = validator.validate("toolong")
        assert len(errors) == 1
        assert "maximaal" in errors[0]

    def test_only_min(self):
        validator = MinMaxLengthValidator(min_length=3)
        assert validator.validate("abc") == []

    def test_only_max(self):
        validator = MinMaxLengthValidator(max_length=10)
        assert validator.validate("short") == []

    def test_exact_min(self):
        validator = MinMaxLengthValidator(min_length=3)
        assert validator.validate("abc") == []

    def test_exact_max(self):
        validator = MinMaxLengthValidator(max_length=5)
        assert validator.validate("12345") == []

    def test_empty_returns_no_errors(self):
        validator = MinMaxLengthValidator(min_length=3)
        assert validator.validate("") == []


class TestRangeValidator:
    def test_within_range(self):
        validator = RangeValidator(min_value=1, max_value=10)
        assert validator.validate(5) == []

    def test_below_min(self):
        validator = RangeValidator(min_value=1)
        errors = validator.validate(0)
        assert len(errors) == 1
        assert "minimaal" in errors[0]

    def test_above_max(self):
        validator = RangeValidator(max_value=10)
        errors = validator.validate(20)
        assert len(errors) == 1
        assert "maximaal" in errors[0]

    def test_string_number(self):
        validator = RangeValidator(min_value=1, max_value=10)
        assert validator.validate("5") == []

    def test_invalid_number(self):
        validator = RangeValidator(min_value=1)
        errors = validator.validate("abc")
        assert len(errors) == 1
        assert "getal" in errors[0]

    def test_empty_returns_no_errors(self):
        validator = RangeValidator(min_value=1)
        assert validator.validate(None) == []
        assert validator.validate("") == []


class TestRequiredValidator:
    def test_valid_string(self):
        assert RequiredValidator().validate("hello") == []

    def test_valid_number(self):
        assert RequiredValidator().validate(42) == []

    def test_valid_list(self):
        assert RequiredValidator().validate(["a"]) == []

    def test_none(self):
        errors = RequiredValidator().validate(None)
        assert len(errors) == 1
        assert "verplicht" in errors[0]

    def test_empty_string(self):
        errors = RequiredValidator().validate("")
        assert len(errors) == 1

    def test_whitespace_string(self):
        errors = RequiredValidator().validate("   ")
        assert len(errors) == 1

    def test_empty_list(self):
        errors = RequiredValidator().validate([])
        assert len(errors) == 1

    def test_false_is_valid(self):
        """Boolean False is a valid value."""
        assert RequiredValidator().validate(False) == []

    def test_zero_is_valid(self):
        """Zero is a valid value."""
        assert RequiredValidator().validate(0) == []
```

## Code Style

- Use `import re` for regex validators
- Use lowercase type hints: `dict`, `list`
- Use `|` for unions: `str | None`
- Use `from __future__ import annotations`
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
