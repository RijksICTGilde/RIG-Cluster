from __future__ import annotations

from opi.forms.editables.validators import (
    EmailValidator,
    MinMaxLengthValidator,
    RangeValidator,
    RequiredValidator,
    SlugValidator,
)


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
