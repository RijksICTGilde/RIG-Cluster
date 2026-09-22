"""Toetsen op de opgeslagen wachtwoordvormen: base64+age, age en plain."""

import base64
import shutil

import pytest
from opi.utils.age import (
    decrypt_age_content_sync,
    decrypt_password_smart_sync,
    encrypt_age_content_sync,
    is_age_encrypted,
    parse_password_with_prefix,
)

#: Een sleutel die het versleutelde wachtwoord in deze toetsen niet opent.
ANDERE_PRIVATE_KEY = "REDACTED-AGE-PRIVATE-KEY-SEE-SECURITY-NOTICE"

WACHTWOORD = "een-wegwerpwachtwoord"


@pytest.fixture
def versleuteld_wachtwoord(age_sleutelpaar: tuple[str, str]) -> str:
    """Het wachtwoord in de vorm waarin de configmap hem draagt: base64 over een age-blok."""
    if shutil.which("age") is None:
        pytest.skip("versleutelen loopt nog via het age-binary")

    publiek, _ = age_sleutelpaar
    blok = encrypt_age_content_sync(WACHTWOORD, publiek)
    return f"base64+age:{base64.b64encode(blok.encode()).decode()}"


class TestAgePasswordDecryption:
    def test_parse_password_with_prefix(self, versleuteld_wachtwoord: str):
        """Test password prefix parsing."""
        # Test base64+age prefix
        password_type, content = parse_password_with_prefix(versleuteld_wachtwoord)
        assert password_type == "base64+age"
        assert content == versleuteld_wachtwoord.removeprefix("base64+age:")

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

    def test_is_age_encrypted(self, versleuteld_wachtwoord: str):
        """Test Age encryption detection."""
        # Decode base64 to get actual Age content
        age_content = base64.b64decode(versleuteld_wachtwoord.removeprefix("base64+age:")).decode("utf-8")

        assert is_age_encrypted(age_content) is True
        assert is_age_encrypted("plain text") is False
        assert is_age_encrypted("") is False

    def test_decrypt_password_smart_sync_base64_age(self, age_sleutelpaar, versleuteld_wachtwoord: str):
        """Test decryption of a base64+age password in the configmap format."""
        _, prive = age_sleutelpaar

        assert decrypt_password_smart_sync(versleuteld_wachtwoord, prive) == WACHTWOORD

    def test_decrypt_password_smart_sync_failure(self, versleuteld_wachtwoord: str):
        """Test handling of decryption failure."""
        # API now raises ValueError on decryption failure
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_password_smart_sync(versleuteld_wachtwoord, ANDERE_PRIVATE_KEY)

    def test_decrypt_password_smart_sync_no_key(self, versleuteld_wachtwoord: str):
        """Test behavior when no private key is provided."""
        # API now raises ValueError when no key is available
        with pytest.raises(ValueError, match="no private key available"):
            decrypt_password_smart_sync(versleuteld_wachtwoord, None)

    def test_decrypt_password_smart_sync_plain_text(self, age_sleutelpaar):
        """Test handling of plain text passwords."""
        _, prive = age_sleutelpaar
        plain_password = "plain:simple_password"
        result = decrypt_password_smart_sync(plain_password, prive)

        # Should return the content without prefix
        assert result == "simple_password"

    def test_configmap_password_integration(self, age_sleutelpaar, versleuteld_wachtwoord: str):
        """De base64+age-vorm uit de configmap en het armored blok erin openen hetzelfde."""
        _, prive = age_sleutelpaar

        result = decrypt_password_smart_sync(versleuteld_wachtwoord, prive)
        armored = base64.b64decode(versleuteld_wachtwoord.removeprefix("base64+age:")).decode("utf-8")

        assert is_age_encrypted(armored)
        assert decrypt_age_content_sync(armored, prive) == result
