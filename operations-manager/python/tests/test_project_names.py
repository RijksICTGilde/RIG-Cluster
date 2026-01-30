"""Tests for opi.utils.project_names module.

Exposes return-type bug in validate_generated_name: re.match() returns
a Match object, not bool, so `is True` fails.
"""

from opi.utils.project_names import validate_generated_name


class TestValidateGeneratedName:
    """validate_generated_name must return a bool, not a re.Match object."""

    def test_returns_true_not_match_object(self):
        """A valid name should return exactly True, not a truthy Match object."""
        result = validate_generated_name("abc-123")
        assert result is True, f"Expected True (bool), got {type(result).__name__}: {result!r}"

    def test_returns_false_for_invalid(self):
        """An invalid name should return exactly False, not None."""
        result = validate_generated_name("123-abc")
        assert result is False, f"Expected False (bool), got {type(result).__name__}: {result!r}"

    def test_returns_false_for_empty(self):
        result = validate_generated_name("")
        assert result is False
