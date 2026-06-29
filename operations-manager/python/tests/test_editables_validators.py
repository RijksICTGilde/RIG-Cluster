from __future__ import annotations

from opi.forms.editables.validators import (
    AttachmentIdValidator,
    ComponentNameValidator,
    ContainerImageValidator,
    EmailValidator,
    MinMaxLengthValidator,
    PathValidator,
    RangeValidator,
    RequiredValidator,
    SlugValidator,
)


class TestAttachmentIdValidator:
    def test_valid_id(self):
        assert AttachmentIdValidator().validate("my-cert-1") == []

    def test_empty_deferred_to_required(self):
        # By convention the format validator skips empty; the staging route pairs
        # it with RequiredValidator so an empty id is rejected (not staged under '').
        assert AttachmentIdValidator().validate("") == []
        assert RequiredValidator().validate("") == ["Dit veld is verplicht"]

    def test_rejects_bad_format(self):
        assert AttachmentIdValidator().validate("1bad") != []

    def test_rejects_duplicate(self):
        errors = AttachmentIdValidator().validate("cert", {"existing_attachment_ids": ["cert"]})
        assert errors != []


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


class TestComponentNameValidator:
    def test_valid_name(self):
        assert ComponentNameValidator().validate("frontend") == []

    def test_valid_short(self):
        assert ComponentNameValidator().validate("api") == []

    def test_valid_with_digits(self):
        assert ComponentNameValidator().validate("app2") == []

    def test_max_length_12(self):
        assert ComponentNameValidator().validate("abcdefghijkl") == []  # 12 chars

    def test_too_long(self):
        errors = ComponentNameValidator().validate("abcdefghijklm")  # 13 chars
        assert len(errors) == 1
        assert "12" in errors[0]

    def test_no_underscores(self):
        errors = ComponentNameValidator().validate("my_app")
        assert len(errors) == 1

    def test_no_hyphens(self):
        errors = ComponentNameValidator().validate("my-app")
        assert len(errors) == 1

    def test_no_spaces(self):
        errors = ComponentNameValidator().validate("my app")
        assert len(errors) == 1

    def test_no_uppercase(self):
        errors = ComponentNameValidator().validate("MyApp")
        assert len(errors) == 1

    def test_must_start_with_letter(self):
        errors = ComponentNameValidator().validate("1app")
        assert len(errors) == 1

    def test_empty_returns_no_errors(self):
        assert ComponentNameValidator().validate("") == []
        assert ComponentNameValidator().validate(None) == []


class TestPathValidator:
    def test_valid_root_path(self):
        assert PathValidator().validate("/") == []

    def test_valid_nested_path(self):
        assert PathValidator().validate("/api/v2") == []

    def test_missing_leading_slash(self):
        errors = PathValidator().validate("api")
        assert len(errors) == 1
        assert "beginnen met /" in errors[0]

    def test_no_spaces(self):
        errors = PathValidator().validate("/my path")
        assert len(errors) == 1
        assert "spaties" in errors[0]

    def test_empty_returns_no_errors(self):
        assert PathValidator().validate("") == []
        assert PathValidator().validate(None) == []


class TestContainerImageValidator:
    def test_valid_image(self):
        assert ContainerImageValidator().validate("nginx:latest") == []

    def test_valid_with_registry(self):
        assert ContainerImageValidator().validate("registry.example.com/app:v1.0") == []

    def test_uppercase_rejected(self):
        errors = ContainerImageValidator().validate("Nginx:latest")
        assert len(errors) == 1
        assert "kleine letters" in errors[0]

    def test_spaces_rejected(self):
        errors = ContainerImageValidator().validate("nginx: latest")
        assert len(errors) == 1
        assert "spaties" in errors[0]

    def test_empty_returns_no_errors(self):
        assert ContainerImageValidator().validate("") == []
        assert ContainerImageValidator().validate(None) == []

    def test_placeholder_brackets_rejected(self):
        # The literal "<sha7>" placeholder that slipped past the old validator.
        errors = ContainerImageValidator().validate("ghcr.io/minbzk/app:main-<sha7>")
        assert errors
        assert any("niet-toegestane tekens" in e for e in errors)
        assert any("<" in e and ">" in e for e in errors)

    def test_html_escaped_value_rejected(self):
        # The multiply-HTML-escaped corruption seen in production.
        errors = ContainerImageValidator().validate("ghcr.io/minbzk/app:main-&amp;amp;lt;sha7&amp;amp;gt;")
        assert any("niet-toegestane tekens" in e for e in errors)

    def test_leading_invalid_char_rejected(self):
        errors = ContainerImageValidator().validate("-nginx:latest")
        assert errors == ["Container image moet beginnen met een letter of cijfer"]

    def test_valid_with_at_digest(self):
        assert ContainerImageValidator().validate("registry/app@sha256:abc123") == []


class TestDeploymentNameValidator:
    """Deployment name must be a Kubernetes-safe label: lowercase, starts with a letter
    (so never all-digits, and out of YAML's int parsing), digits/hyphens, max 63."""

    def test_rejects_spaces_and_capitals(self):
        from opi.forms.editables.validators import DeploymentNameValidator

        assert DeploymentNameValidator().validate("MOZa Dienstverlenerportaal PoC")

    def test_rejects_all_digits_and_leading_digit(self):
        from opi.forms.editables.validators import DeploymentNameValidator

        v = DeploymentNameValidator()
        assert v.validate("123")
        assert v.validate("1abc")

    def test_rejects_too_long(self):
        from opi.forms.editables.validators import DeploymentNameValidator

        assert DeploymentNameValidator().validate("a" * 64)

    def test_accepts_valid_names(self):
        from opi.forms.editables.validators import DeploymentNameValidator

        v = DeploymentNameValidator()
        assert v.validate("productie") == []
        assert v.validate("mozad-dle") == []
        assert v.validate("a") == []

    def test_empty_is_not_a_format_error(self):
        from opi.forms.editables.validators import DeploymentNameValidator

        # Emptiness is enforced by required=True, not here.
        assert DeploymentNameValidator().validate("") == []
