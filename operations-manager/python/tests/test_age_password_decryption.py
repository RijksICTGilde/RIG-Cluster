"""
Test Age password decryption using the GIT_PROJECTS_SERVER_PASSWORD from configmap.
"""

import base64

import pytest
from opi.utils.age import (
    decrypt_age_content_sync,
    decrypt_password_smart_sync,
    is_age_encrypted,
    parse_password_with_prefix,
)

#: Een sleutel die het versleutelde wachtwoord in deze toetsen niet opent.
ANDERE_PRIVATE_KEY = "REDACTED-AGE-PRIVATE-KEY-SEE-SECURITY-NOTICE"


class TestAgePasswordDecryption:
    """Test Age encryption/decryption functionality with real configmap data."""

    def setup_method(self):
        """Setup test data from configmap."""
        # Password from configmap.yaml (base64+age format)
        self.encrypted_password = "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NBMEsyOHpaRVJ4WjI5Wk1qVnVRVk5QCldFcE1VMHd3TVhOUE4yRjFUM1pTSzJNNVRtTjRiM1JOWTNkbkNtaExOM0Z4THpjdk4wMU9kbUl4V1hWRkwwMHoKZEN0MEwwZHJjVkZaVVRCS09FUklaM05RSzNWRlVHY0tMUzB0SUZOQ1FVTTNaMVUwTUdKM2VUWXhlQzlUYjI5WgpabXhUV205QlJHdHBVRXhVVmxOM04xSlBValJoVjBrS3Q5NmxiY1NPcUxUaEVndnI2N1BrM2k0SUJWNmo4bVBvCkFUVGFIdjNDTUtjTVFPckRjSjRaMmlsTDZDZ0IvUlV3KzVHM21CWi9BMGYxbjVIZHFZZlhmTGk4c2xZNzM0OFMKRFE9PQotLS0tLUVORCBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="

        # Private key from security/key.txt
        self.private_key = "REDACTED-AGE-PRIVATE-KEY-SEE-SECURITY-NOTICE"

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

    def test_parse_password_none_returns_string_content(self):
        """parse_password_with_prefix(None) must return string content, not None."""
        password_type, content = parse_password_with_prefix(None)
        assert password_type == "plain"
        assert isinstance(content, str), "Content must be a string, not None"

    def test_parse_password_empty_age_prefix_returns_plain(self):
        """parse_password_with_prefix('age:') with no content should fall back to plain, not return age type."""
        password_type, content = parse_password_with_prefix("age:")
        assert password_type == "plain", "age: with no content is not valid encrypted data, should be treated as plain"

    def test_is_age_encrypted(self):
        """Test Age encryption detection."""
        # Decode base64 to get actual Age content
        base64_content = "LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NBMEsyOHpaRVJ4WjI5Wk1qVnVRVk5QCldFcE1VMHd3TVhOUE4yRjFUM1pTSzJNNVRtTjRiM1JOWTNkbkNtaExOM0Z4THpjdk4wMU9kbUl4V1hWRkwwMHoKZEN0MEwwZHJjVkZaVVRCS09FUklaM05RSzNWRlVHY0tMUzB0SUZOQ1FVTTNaMVUwTUdKM2VUWXhlQzlUYjI5WgpabXhUV205QlJHdHBVRXhVVmxOM04xSlBValJoVjBrS3Q5NmxiY1NPcUxUaEVndnI2N1BrM2k0SUJWNmo4bVBvCkFUVGFIdjNDTUtjTVFPckRjSjRaMmlsTDZDZ0IvUlV3KzVHM21CWi9BMGYxbjVIZHFZZlhmTGk4c2xZNzM0OFMKRFE9PQotLS0tLUVORCBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="
        age_content = base64.b64decode(base64_content).decode("utf-8")

        assert is_age_encrypted(age_content) is True
        assert is_age_encrypted("plain text") is False
        assert is_age_encrypted("") is False

    def test_decrypt_password_smart_sync_base64_age(self):
        """Test decryption of base64+age password from configmap."""
        result = decrypt_password_smart_sync(self.encrypted_password, self.private_key)

        assert len(result) == 40
        assert not result.startswith("base64+age:")

    def test_decrypt_password_smart_sync_failure(self):
        """Test handling of decryption failure."""
        # API now raises ValueError on decryption failure
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_password_smart_sync(self.encrypted_password, ANDERE_PRIVATE_KEY)

    def test_decrypt_password_smart_sync_no_key(self):
        """Test behavior when no private key is provided."""
        # API now raises ValueError when no key is available
        with pytest.raises(ValueError, match="no private key available"):
            decrypt_password_smart_sync(self.encrypted_password, None)

    def test_decrypt_password_smart_sync_plain_text(self):
        """Test handling of plain text passwords."""
        plain_password = "plain:simple_password"
        result = decrypt_password_smart_sync(plain_password, self.private_key)

        # Should return the content without prefix
        assert result == "simple_password"

    def test_configmap_password_integration(self):
        """De base64+age-vorm uit de configmap en het armored blok erin openen hetzelfde."""
        configmap_password = "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NBMEsyOHpaRVJ4WjI5Wk1qVnVRVk5QCldFcE1VMHd3TVhOUE4yRjFUM1pTSzJNNVRtTjRiM1JOWTNkbkNtaExOM0Z4THpjdk4wMU9kbUl4V1hWRkwwMHoKZEN0MEwwZHJjVkZaVVRCS09FUklaM05RSzNWRlVHY0tMUzB0SUZOQ1FVTTNaMVUwTUdKM2VUWXhlQzlUYjI5WgpabXhUV205QlJHdHBVRXhVVmxOM04xSlBValJoVjBrS3Q5NmxiY1NPcUxUaEVndnI2N1BrM2k0SUJWNmo4bVBvCkFUVGFIdjNDTUtjTVFPckRjSjRaMmlsTDZDZ0IvUlV3KzVHM21CWi9BMGYxbjVIZHFZZlhmTGk4c2xZNzM0OFMKRFE9PQotLS0tLUVORCBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="

        # Test decryption with key from security/key.txt
        result = decrypt_password_smart_sync(configmap_password, self.private_key)
        armored = base64.b64decode(configmap_password.removeprefix("base64+age:")).decode("utf-8")

        assert is_age_encrypted(armored)
        assert decrypt_age_content_sync(armored, self.private_key) == result
