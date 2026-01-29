"""Tests for opi.utils.passwords module."""

import string

import pytest
from opi.utils.passwords import generate_alphanumeric_password, generate_secure_password


class TestGenerateSecurePassword:
    """Tests for generate_secure_password."""

    def test_default_length(self):
        password = generate_secure_password()
        assert len(password) == 20

    def test_default_has_min_uppercase(self):
        password = generate_secure_password()
        uppercase_count = sum(1 for c in password if c in string.ascii_uppercase)
        assert uppercase_count >= 3

    def test_default_has_min_lowercase(self):
        password = generate_secure_password()
        lowercase_count = sum(1 for c in password if c in string.ascii_lowercase)
        assert lowercase_count >= 3

    def test_default_has_min_digits(self):
        password = generate_secure_password()
        digit_count = sum(1 for c in password if c in string.digits)
        assert digit_count >= 3

    def test_custom_min_uppercase(self):
        password = generate_secure_password(min_uppercase=5, total_length=20)
        uppercase_count = sum(1 for c in password if c in string.ascii_uppercase)
        assert uppercase_count >= 5

    def test_custom_min_lowercase(self):
        password = generate_secure_password(min_lowercase=7, total_length=20)
        lowercase_count = sum(1 for c in password if c in string.ascii_lowercase)
        assert lowercase_count >= 7

    def test_custom_min_digits(self):
        password = generate_secure_password(min_digits=6, total_length=20)
        digit_count = sum(1 for c in password if c in string.digits)
        assert digit_count >= 6

    def test_custom_total_length(self):
        password = generate_secure_password(total_length=30)
        assert len(password) == 30

    def test_valueerror_when_minimums_exceed_length(self):
        with pytest.raises(ValueError, match="Minimum character requirements"):
            generate_secure_password(min_uppercase=5, min_lowercase=5, min_digits=5, total_length=10)

    def test_valueerror_exact_boundary(self):
        # Minimums sum to exactly total_length + 1
        with pytest.raises(ValueError, match="Minimum character requirements"):
            generate_secure_password(min_uppercase=4, min_lowercase=4, min_digits=4, total_length=11)

    def test_minimums_equal_total_length(self):
        # Should not raise when minimums exactly equal total_length
        password = generate_secure_password(min_uppercase=4, min_lowercase=3, min_digits=3, total_length=10)
        assert len(password) == 10

    def test_additional_chars_can_appear(self):
        additional = "!@#$%"
        # Generate many passwords to increase chance of seeing additional chars
        passwords = [generate_secure_password(additional_chars=additional, total_length=50) for _ in range(20)]
        all_chars = "".join(passwords)
        assert any(c in all_chars for c in additional)

    def test_additional_chars_length_unchanged(self):
        password = generate_secure_password(additional_chars="!@#$%", total_length=25)
        assert len(password) == 25

    def test_randomness_produces_different_results(self):
        passwords = {generate_secure_password() for _ in range(10)}
        assert len(passwords) > 1

    def test_returns_string(self):
        password = generate_secure_password()
        assert isinstance(password, str)


class TestGenerateAlphanumericPassword:
    """Tests for generate_alphanumeric_password."""

    def test_default_length(self):
        password = generate_alphanumeric_password()
        assert len(password) == 20

    def test_custom_length_10(self):
        password = generate_alphanumeric_password(length=10)
        assert len(password) == 10

    def test_custom_length_30(self):
        password = generate_alphanumeric_password(length=30)
        assert len(password) == 30

    def test_contains_uppercase(self):
        password = generate_alphanumeric_password()
        assert any(c in string.ascii_uppercase for c in password)

    def test_contains_lowercase(self):
        password = generate_alphanumeric_password()
        assert any(c in string.ascii_lowercase for c in password)

    def test_contains_digits(self):
        password = generate_alphanumeric_password()
        assert any(c in string.digits for c in password)

    def test_only_alphanumeric_characters(self):
        password = generate_alphanumeric_password()
        assert all(c in string.ascii_letters + string.digits for c in password)

    def test_returns_string(self):
        password = generate_alphanumeric_password()
        assert isinstance(password, str)

    def test_randomness_produces_different_results(self):
        passwords = {generate_alphanumeric_password() for _ in range(10)}
        assert len(passwords) > 1
