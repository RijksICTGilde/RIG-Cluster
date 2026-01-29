"""Tests for opi.manager.project_manager module.

Focuses on: async correctness, command construction, edge cases in deployment processing.
"""

import inspect

import pytest
from opi.manager.project_manager import ProjectManager


class TestAsyncCorrectness:
    """All calls to async functions must use await — missing await silently returns a coroutine object."""

    def test_decrypt_age_content_calls_are_awaited(self):
        """Every call to decrypt_age_content in project_manager must be awaited.

        Missing await causes the coroutine to be stringified as '<coroutine object ...>'
        instead of the actual decrypted value.
        """
        source = inspect.getsource(ProjectManager)

        # Find all lines with decrypt_age_content calls
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "decrypt_age_content(" in stripped and not stripped.startswith("#"):
                # Skip import lines
                if "import" in stripped:
                    continue
                assert "await" in stripped, (
                    f"Line {i}: decrypt_age_content() is async but called without await: {stripped}"
                )


class TestMissingFStrings:
    """Strings with {var} placeholders must be f-strings, otherwise the variable is not interpolated."""

    def test_storage_type_error_includes_actual_type(self):
        """ValueError for unknown storage type must include the actual type value, not literal '{storage_type}'."""
        pm = ProjectManager.__new__(ProjectManager)
        with pytest.raises(ValueError, match="bogus_type"):
            pm._generate_storage_env_vars_from_services(
                [{"mount-path": "/data", "type": "bogus_type"}]
            )

    def test_no_deployments_warning_includes_project_name(self):
        """Log strings with {var} placeholders must use f-string prefix to interpolate variables."""
        source = inspect.getsource(ProjectManager.check_and_create_sops_secrets_in_namespaces)
        # Find the warning log line about no deployments
        for line in source.split("\n"):
            stripped = line.strip()
            if "No deployments found in project" in stripped and "logger" in stripped:
                # The string should be an f-string so {project_name} gets interpolated
                assert 'f"' in stripped or "f'" in stripped, (
                    f"Missing f-prefix on string with {{project_name}} placeholder: {stripped}"
                )
