"""
Tests for age encryption functionality in opi.utils.age.

Complements test_age_password_decryption.py by covering encrypt_age_content,
_encrypt_with_age_and_base64encode_as_prefixed_string, and edge cases.
"""

import base64
from unittest.mock import AsyncMock, patch

import pytest
from opi.utils.age import (
    _encrypt_with_age_and_base64encode_as_prefixed_string,
    decrypt_if_encrypted,
    encrypt_age_content,
    get_project_public_key,
    is_age_encrypted,
)


class TestEncryptAgeContent:
    """Tests for encrypt_age_content."""

    @pytest.mark.asyncio
    async def test_encrypt_success(self):
        """Should encrypt content and return armored output."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(b"-----BEGIN AGE ENCRYPTED FILE-----\nencrypted\n-----END AGE ENCRYPTED FILE-----", b"")
        )
        mock_process.returncode = 0

        with patch("opi.utils.age.asyncio.create_subprocess_exec", return_value=mock_process):
            result = await encrypt_age_content("secret", "age1publickey")

        assert "BEGIN AGE ENCRYPTED FILE" in result

    @pytest.mark.asyncio
    async def test_encrypt_missing_public_key(self):
        """Should raise ValueError when public key is None."""
        with pytest.raises(ValueError, match="Missing public age key"):
            await encrypt_age_content("secret", None)

    @pytest.mark.asyncio
    async def test_encrypt_empty_public_key(self):
        """Should raise ValueError when public key is empty string."""
        with pytest.raises(ValueError, match="Missing public age key"):
            await encrypt_age_content("secret", "")

    @pytest.mark.asyncio
    async def test_encrypt_missing_content(self):
        """Should raise ValueError when content is empty."""
        with pytest.raises(ValueError, match="Missing plain content"):
            await encrypt_age_content("", "age1publickey")

    @pytest.mark.asyncio
    async def test_encrypt_process_failure(self):
        """Should raise Exception when age process fails."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"age: error: bad key"))
        mock_process.returncode = 1

        with (
            patch("opi.utils.age.asyncio.create_subprocess_exec", return_value=mock_process),
            pytest.raises(Exception, match="Age encryption failed"),
        ):
            await encrypt_age_content("secret", "age1publickey")


class TestEncryptWithAgeAndBase64Encode:
    """Tests for _encrypt_with_age_and_base64encode_as_prefixed_string."""

    @pytest.mark.asyncio
    async def test_returns_prefixed_base64_string(self):
        """Should return base64+age: prefixed string."""
        encrypted = "-----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----"
        with patch("opi.utils.age.encrypt_age_content", return_value=encrypted):
            result = await _encrypt_with_age_and_base64encode_as_prefixed_string("my_secret", "age1pub")

        assert result.startswith("base64+age:")
        # Decode the base64 part and verify
        b64_part = result[len("base64+age:") :]
        decoded = base64.b64decode(b64_part).decode()
        assert decoded == encrypted


class TestDecryptIfEncrypted:
    """Tests for decrypt_if_encrypted."""

    @pytest.mark.asyncio
    async def test_returns_plain_text_unchanged(self):
        """Should return non-encrypted content as-is."""
        result = await decrypt_if_encrypted("plain text", "some-key")
        assert result == "plain text"

    @pytest.mark.asyncio
    async def test_raises_without_private_key(self):
        """Should raise ValueError when content is encrypted but no key provided."""
        encrypted = "-----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----"
        with pytest.raises(ValueError, match="no private key"):
            await decrypt_if_encrypted(encrypted, None)

    @pytest.mark.asyncio
    async def test_decrypts_encrypted_content(self):
        """Should decrypt age-encrypted content."""
        encrypted = "-----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----"
        with patch("opi.utils.age.decrypt_age_content", return_value="decrypted"):
            result = await decrypt_if_encrypted(encrypted, "AGE-SECRET-KEY-1ABC")
        assert result == "decrypted"


class TestIsAgeEncrypted:
    """Tests for is_age_encrypted edge cases."""

    def test_empty_string(self):
        assert is_age_encrypted("") is False

    def test_none_like(self):
        assert is_age_encrypted("") is False

    def test_only_begin_marker(self):
        assert is_age_encrypted("-----BEGIN AGE ENCRYPTED FILE-----") is False

    def test_only_end_marker(self):
        assert is_age_encrypted("-----END AGE ENCRYPTED FILE-----") is False

    def test_valid_with_whitespace(self):
        content = "  -----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----  "
        assert is_age_encrypted(content) is True


class TestGetProjectPublicKey:
    """Tests for get_project_public_key."""

    def test_new_key_name(self):
        """Should return key from age-public-key."""
        config = {"config": {"age-public-key": "age1newkey"}}
        assert get_project_public_key(config) == "age1newkey"

    def test_legacy_key_name(self):
        """Should fallback to sops-public-key."""
        config = {"config": {"sops-public-key": "age1legacykey"}}
        assert get_project_public_key(config) == "age1legacykey"

    def test_new_key_takes_precedence(self):
        """New key name should take precedence over legacy."""
        config = {"config": {"age-public-key": "age1new", "sops-public-key": "age1old"}}
        assert get_project_public_key(config) == "age1new"

    def test_no_key_found(self):
        """Should return None when no key is present."""
        config = {"config": {}}
        assert get_project_public_key(config) is None

    def test_empty_config(self):
        """Should return None for empty config dict."""
        config = {}
        assert get_project_public_key(config) is None
