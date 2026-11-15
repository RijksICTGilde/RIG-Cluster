"""Tests for opi.utils.project_names module.

Guards the return type of validate_project_name: re.match() gives a Match object, not a
bool, so a caller writing ``is True`` breaks on a plain ``return re.match(...)``.
"""

from opi.utils.project_names import validate_project_name


class TestValidateProjectName:
    """validate_project_name must return a bool, not a re.Match object."""

    def test_returns_true_not_match_object(self):
        """A valid name should return exactly True, not a truthy Match object."""
        result = validate_project_name("abc-123")
        assert result is True, f"Expected True (bool), got {type(result).__name__}: {result!r}"

    def test_returns_false_for_invalid(self):
        """An invalid name should return exactly False, not None."""
        result = validate_project_name("123-abc")
        assert result is False, f"Expected False (bool), got {type(result).__name__}: {result!r}"

    def test_returns_false_for_empty(self):
        result = validate_project_name("")
        assert result is False
