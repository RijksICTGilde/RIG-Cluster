"""
Test for ENV format detection in project_file_handler.

This test verifies that ENV format (KEY=VALUE) is correctly detected
and not incorrectly parsed as YAML.
"""

from opi.handlers.project_file_handler import ProjectFileHandler


class TestEnvFormatDetection:
    """Test ENV format detection."""

    def test_env_format_single_line(self):
        """Test ENV format detection with single line."""
        handler = ProjectFileHandler()

        env_content = "NUXT_PUBLIC_API_BASE_URL=/api"

        assert handler._looks_like_env_format(env_content) is True

    def test_env_format_multiple_lines(self):
        """Test ENV format detection with multiple lines."""
        handler = ProjectFileHandler()

        env_content = """NUXT_PUBLIC_API_BASE_URL=/api
NUXT_AANLEVER_BASE_URL=/aanleverapi"""

        assert handler._looks_like_env_format(env_content) is True

    def test_env_format_with_comments(self):
        """Test ENV format detection with comments."""
        handler = ProjectFileHandler()

        env_content = """# Configuration
NUXT_PUBLIC_API_BASE_URL=/api
NUXT_AANLEVER_BASE_URL=/aanleverapi"""

        assert handler._looks_like_env_format(env_content) is True

    def test_yaml_format_not_detected_as_env(self):
        """Test that YAML format is not detected as ENV."""
        handler = ProjectFileHandler()

        yaml_content = """api_url: /api
base_url: /aanleverapi"""

        assert handler._looks_like_env_format(yaml_content) is False

    def test_empty_string(self):
        """Test empty string returns False."""
        handler = ProjectFileHandler()

        assert handler._looks_like_env_format("") is False
        assert handler._looks_like_env_format(None) is False

    def test_lowercase_env_var(self):
        """Test ENV format with lowercase var (should still match)."""
        handler = ProjectFileHandler()

        env_content = "myVar=value"

        assert handler._looks_like_env_format(env_content) is True

    def test_underscore_prefix(self):
        """Test ENV format with underscore prefix."""
        handler = ProjectFileHandler()

        env_content = "_PRIVATE_VAR=secret"

        assert handler._looks_like_env_format(env_content) is True

    def test_check_only_first_two_lines(self):
        """Test that only first 2 content lines are checked."""
        handler = ProjectFileHandler()

        # First line is ENV format, should return True immediately
        env_content = """DATABASE_URL=postgres://localhost
this is not env format
and neither is this"""

        assert handler._looks_like_env_format(env_content) is True
