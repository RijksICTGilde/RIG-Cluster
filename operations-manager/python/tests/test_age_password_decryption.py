"""
Test Age password decryption using the GIT_PROJECTS_SERVER_PASSWORD from configmap.
"""

import base64
from unittest.mock import patch

from opi.utils.age import decrypt_password_smart_sync, is_age_encrypted, parse_password_with_prefix


class TestAgePasswordDecryption:
    """Test Age encryption/decryption functionality with real configmap data."""

    def setup_method(self):
        """Setup test data from configmap."""
        # Password from configmap.yaml (base64+age format)
        self.encrypted_password = "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NBMEsyOHpaRVJ4WjI5Wk1qVnVRVk5QCldFcE1VMHd3TVhOUE4yRjFUM1pTSzJNNVRtTjRiM1JOWTNkbkNtaExOM0Z4THpjdk4wMU9kbUl4V1hWRkwwMHoKZEN0MEwwZHJjVkZaVVRCS09FUklaM05RSzNWRlVHY0tMUzB0SUZOQ1FVTTNaMVUwTUdKM2VUWXhlQzlUYjI5WgpabXhUV205QlJHdHBVRXhVVmxOM04xSlBValJoVjBrS3Q5NmxiY1NPcUxUaEVndnI2N1BrM2k0SUJWNmo4bVBvCkFUVGFIdjNDTUtjTVFPckRjSjRaMmlsTDZDZ0IvUlV3KzVHM21CWi9BMGYxbjVIZHFZZlhmTGk4c2xZNzM0OFMKRFE9PQotLS0tLUVORCBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="

        # Private key from security/key.txt
        self.private_key = "AGE-SECRET-KEY-1KDGL6ZRZRXLK40KPX66RXA9ADMNZ7AH7PHGX8CCX8J23ZSMYFHAS35H5PJ"

    def test_parse_password_with_prefix(self):
        """Test password prefix parsing."""
        # Test base64+age prefix
        password_type, content = parse_password_with_prefix(self.encrypted_password)
        assert password_type == "base64+age"
        assert (
            content
            == "LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NBMEsyOHpaRVJ4WjI5Wk1qVnVRVk5QCldFcE1VMHd3TVhOUE4yRjFUM1pTSzJNNVRtTjRiM1JOWTNkbkNtaExOM0Z4THpjdk4wMU9kbUl4V1hWRkwwMHoKZEN0MEwwZHJjVkZaVVRCS09FUklaM05RSzNWRlVHY0tMUzB0SUZOQ1FVTTNaMVUwTUdKM2VUWXhlQzlUYjI5WgpabXhUV205QlJHdHBVRXhVVmxOM04xSlBValJoVjBrS3Q5NmxiY1NPcUxUaEVndnI2N1BrM2k0SUJWNmo4bVBvCkFUVGFIdjNDTUtjTVFPckRjSjRaMmlsTDZDZ0IvUlV3KzVHM21CWi9BMGYxbjVIZHFZZlhmTGk4c2xZNzM0OFMKRFE9PQotLS0tLUVORCBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="
        )

        # Test plain prefix
        plain_type, plain_content = parse_password_with_prefix("plain:test123")
        assert plain_type == "plain"
        assert plain_content == "test123"

        # Test age prefix
        age_content = "-----BEGIN AGE ENCRYPTED FILE-----\ntest\n-----END AGE ENCRYPTED FILE-----"
        age_type, extracted_content = parse_password_with_prefix(f"age:{age_content}")
        assert age_type == "age"
        assert extracted_content == age_content

    def test_is_age_encrypted(self):
        """Test Age encryption detection."""
        # Decode base64 to get actual Age content
        base64_content = "LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NBMEsyOHpaRVJ4WjI5Wk1qVnVRVk5QCldFcE1VMHd3TVhOUE4yRjFUM1pTSzJNNVRtTjRiM1JOWTNkbkNtaExOM0Z4THpjdk4wMU9kbUl4V1hWRkwwMHoKZEN0MEwwZHJjVkZaVVRCS09FUklaM05RSzNWRlVHY0tMUzB0SUZOQ1FVTTNaMVUwTUdKM2VUWXhlQzlUYjI5WgpabXhUV205QlJHdHBVRXhVVmxOM04xSlBValJoVjBrS3Q5NmxiY1NPcUxUaEVndnI2N1BrM2k0SUJWNmo4bVBvCkFUVGFIdjNDTUtjTVFPckRjSjRaMmlsTDZDZ0IvUlV3KzVHM21CWi9BMGYxbjVIZHFZZlhmTGk4c2xZNzM0OFMKRFE9PQotLS0tLUVORCBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="
        age_content = base64.b64decode(base64_content).decode("utf-8")

        assert is_age_encrypted(age_content) is True
        assert is_age_encrypted("plain text") is False
        assert is_age_encrypted("") is False

    @patch("subprocess.run")
    def test_decrypt_password_smart_sync_base64_age(self, mock_subprocess):
        """Test decryption of base64+age password from configmap."""
        # Mock successful age decryption
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = "decrypted_password_123"
        mock_subprocess.return_value.stderr = ""

        # Test decryption
        result = decrypt_password_smart_sync(self.encrypted_password, self.private_key)

        # Verify subprocess was called with age command
        assert mock_subprocess.called
        call_args = mock_subprocess.call_args[0][0]
        assert "bash" in call_args
        assert "age -d" in call_args[2]

        # Verify result
        assert result == "decrypted_password_123"

    @patch("subprocess.run")
    def test_decrypt_password_smart_sync_failure(self, mock_subprocess):
        """Test handling of decryption failure."""
        # Mock failed age decryption
        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = "age: error: decryption failed"

        # Test decryption - should return original password on failure
        result = decrypt_password_smart_sync(self.encrypted_password, self.private_key)

        # Should return original password when decryption fails
        assert result == self.encrypted_password

    def test_decrypt_password_smart_sync_no_key(self):
        """Test behavior when no private key is provided."""
        result = decrypt_password_smart_sync(self.encrypted_password, None)

        # Should return original password when no key available
        assert result == self.encrypted_password

    def test_decrypt_password_smart_sync_plain_text(self):
        """Test handling of plain text passwords."""
        plain_password = "plain:simple_password"
        result = decrypt_password_smart_sync(plain_password, self.private_key)

        # Should return the content without prefix
        assert result == "simple_password"

    @patch("subprocess.run")
    def test_configmap_password_integration(self, mock_subprocess):
        """Integration test using actual configmap password format."""
        # Mock successful decryption
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = "github_pat_12345"
        mock_subprocess.return_value.stderr = ""

        # Use the exact password from configmap
        configmap_password = "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NBMEsyOHpaRVJ4WjI5Wk1qVnVRVk5QCldFcE1VMHd3TVhOUE4yRjFUM1pTSzJNNVRtTjRiM1JOWTNkbkNtaExOM0Z4THpjdk4wMU9kbUl4V1hWRkwwMHoKZEN0MEwwZHJjVkZaVVRCS09FUklaM05RSzNWRlVHY0tMUzB0SUZOQ1FVTTNaMVUwTUdKM2VUWXhlQzlUYjI5WgpabXhUV205QlJHdHBVRXhVVmxOM04xSlBValJoVjBrS3Q5NmxiY1NPcUxUaEVndnI2N1BrM2k0SUJWNmo4bVBvCkFUVGFIdjNDTUtjTVFPckRjSjRaMmlsTDZDZ0IvUlV3KzVHM21CWi9BMGYxbjVIZHFZZlhmTGk4c2xZNzM0OFMKRFE9PQotLS0tLUVORCBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="

        # Test decryption with key from security/key.txt
        result = decrypt_password_smart_sync(configmap_password, self.private_key)

        # Verify the process
        assert mock_subprocess.called
        assert result == "github_pat_12345"

        # Verify the age command structure
        call_args = mock_subprocess.call_args[0][0]
        assert len(call_args) == 3  # ["bash", "-c", "command"]
        assert "age -d -i" in call_args[2]
        assert self.private_key in call_args[2]
