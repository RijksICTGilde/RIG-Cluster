"""Test Age password decryption on a value in the ``base64+age:`` form the env files use.

Every decryption here is mocked (``@patch("subprocess.run")``), so what is measured is the
parsing and the dispatch, not age itself. The encrypted value and the key are therefore test
DATA, and they are made per run by ``age_keypair`` rather than pasted in.

That is not cosmetic. Until the platform key rotation, this file carried the real production
private key on one line, with a comment saying where it came from ("from security/key.txt"), and
the copy of the configmap value it opened right above it. Three other test files carried a fixed
key too, which is why nobody read past it. A test that needs a key now makes one.
"""

import base64
import shutil
from unittest.mock import patch

import pytest
from opi.utils.age import (
    decrypt_age_content_sync,
    decrypt_password_smart_sync,
    encrypt_age_content_sync,
    is_age_encrypted,
    parse_password_with_prefix,
)

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="requires the age and age-keygen binaries to mint the throwaway keypair",
)


@pytest.fixture
def age_block(age_keypair: tuple[str, str]) -> str:
    """An armored AGE block, encrypted for the throwaway key of this run."""
    _private_key, public_key = age_keypair
    return encrypt_age_content_sync("github_pat_12345", public_key)


@pytest.fixture
def base64_age_password(age_block: str) -> str:
    """That same block in the single-line form an env file or a configmap holds."""
    return f"base64+age:{base64.b64encode(age_block.encode()).decode()}"


@pytest.fixture
def private_key(age_keypair: tuple[str, str]) -> str:
    return age_keypair[0]


class TestAgePasswordDecryption:
    """Test Age encryption/decryption functionality on a real ``base64+age:`` value."""

    def test_parse_password_with_prefix(self, base64_age_password: str) -> None:
        """Test password prefix parsing."""
        # Test base64+age prefix: the content is everything after the prefix, untouched.
        password_type, content = parse_password_with_prefix(base64_age_password)
        assert password_type == "base64+age"
        assert content == base64_age_password[len("base64+age:") :]

        # Test plain prefix
        plain_type, plain_content = parse_password_with_prefix("plain:test123")
        assert plain_type == "plain"
        assert plain_content == "test123"

        # Test age prefix
        age_content = "-----BEGIN AGE ENCRYPTED FILE-----\ntest\n-----END AGE ENCRYPTED FILE-----"
        age_type, extracted_content = parse_password_with_prefix(f"age:{age_content}")
        assert age_type == "age"
        assert extracted_content == age_content

    def test_parse_password_none_returns_string_content(self) -> None:
        """parse_password_with_prefix(None) must return string content, not None."""
        password_type, content = parse_password_with_prefix(None)
        assert password_type == "plain"
        assert isinstance(content, str), "Content must be a string, not None"

    def test_parse_password_empty_age_prefix_returns_plain(self) -> None:
        """parse_password_with_prefix('age:') with no content should fall back to plain, not return age type."""
        password_type, _content = parse_password_with_prefix("age:")
        assert password_type == "plain", "age: with no content is not valid encrypted data, should be treated as plain"

    def test_is_age_encrypted(self, base64_age_password: str) -> None:
        """Test Age encryption detection."""
        # Decode base64 to get actual Age content
        base64_content = base64_age_password[len("base64+age:") :]
        age_content = base64.b64decode(base64_content).decode("utf-8")

        assert is_age_encrypted(age_content) is True
        assert is_age_encrypted("plain text") is False
        assert is_age_encrypted("") is False

    @patch("subprocess.run")
    def test_decrypt_password_smart_sync_base64_age(
        self, mock_subprocess: patch, base64_age_password: str, private_key: str
    ) -> None:
        """Test decryption of a base64+age password as it appears in a configmap."""
        # Mock successful age decryption
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = "decrypted_password_123"
        mock_subprocess.return_value.stderr = ""

        # Test decryption
        result = decrypt_password_smart_sync(base64_age_password, private_key)

        # Verify subprocess was called with age command
        assert mock_subprocess.called
        call_args = mock_subprocess.call_args[0][0]
        assert call_args[0] == "age"
        assert "-d" in call_args
        assert "-i" in call_args

        # Verify result
        assert result == "decrypted_password_123"

    @patch("subprocess.run")
    def test_decrypt_password_smart_sync_failure(
        self, mock_subprocess: patch, base64_age_password: str, private_key: str
    ) -> None:
        """Test handling of decryption failure."""
        # Mock failed age decryption
        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = "age: error: decryption failed"

        # API now raises ValueError on decryption failure
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_password_smart_sync(base64_age_password, private_key)

    def test_decrypt_password_smart_sync_no_key(self, base64_age_password: str) -> None:
        """Test behavior when no private key is provided."""
        # API now raises ValueError when no key is available
        with pytest.raises(ValueError, match="no private key available"):
            decrypt_password_smart_sync(base64_age_password, None)

    def test_decrypt_password_smart_sync_plain_text(self, private_key: str) -> None:
        """Test handling of plain text passwords."""
        plain_password = "plain:simple_password"
        result = decrypt_password_smart_sync(plain_password, private_key)

        # Should return the content without prefix
        assert result == "simple_password"

    def test_the_value_really_opens_with_the_key_of_this_run(self, base64_age_password: str, private_key: str) -> None:
        """One unmocked round, so the mocked tests above are not sitting on broken test data.

        Without this, a fixture that produced a value the key cannot open would leave every
        other test in this file green, because they all mock the decryption away.
        """
        block = base64.b64decode(base64_age_password[len("base64+age:") :]).decode()
        assert decrypt_age_content_sync(block, private_key) == "github_pat_12345"
