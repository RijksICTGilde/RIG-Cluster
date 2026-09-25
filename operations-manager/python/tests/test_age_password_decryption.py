"""Toetsen op de opgeslagen wachtwoordvormen: base64+age, age en plain.

De sleutel en het versleutelde wachtwoord zijn test-DATA en worden per run gemaakt, niet in
de boom gezet. Dat is niet cosmetisch: tot de rotatie van de platformsleutel droeg dit bestand
de echte productie-private-sleutel op een regel, met een comment erbij waar hij vandaan kwam
("from security/key.txt") en de kopie van de configmap-waarde die hij opent er vlak boven.
Drie andere toetsbestanden droegen ook een vaste sleutel, en daardoor las niemand er nog langs.
Een toets die een sleutel nodig heeft, maakt er een.

Er wordt hier niets gemockt: ontsleutelen loopt sinds RC-218 via pyrage en start geen proces
meer, dus een mock op ``subprocess.run`` zou niets meer tegenhouden en niets meer meten.
"""

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

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="het wegwerpsleutelpaar komt van age-keygen en versleutelen loopt nog via het age-binary",
)

WACHTWOORD = "een-wegwerpwachtwoord"


@pytest.fixture
def prive_sleutel(age_keypair: tuple[str, str]) -> str:
    private_key, _public_key = age_keypair
    return private_key


@pytest.fixture
def andere_prive_sleutel(make_age_keypair) -> str:
    """Een tweede paar, zodat de sleutel die het wachtwoord NIET opent ook uit age-keygen komt."""
    private_key, _public_key = make_age_keypair()
    return private_key


@pytest.fixture
def versleuteld_wachtwoord(age_keypair: tuple[str, str]) -> str:
    """Het wachtwoord in de vorm waarin de configmap hem draagt: base64 over een age-blok."""
    _private_key, public_key = age_keypair
    blok = encrypt_age_content_sync(WACHTWOORD, public_key)
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

    def test_decrypt_password_smart_sync_base64_age(self, prive_sleutel: str, versleuteld_wachtwoord: str):
        """Test decryption of a base64+age password in the configmap format."""
        assert decrypt_password_smart_sync(versleuteld_wachtwoord, prive_sleutel) == WACHTWOORD

    def test_decrypt_password_smart_sync_failure(self, andere_prive_sleutel: str, versleuteld_wachtwoord: str):
        """Test handling of decryption failure."""
        # API now raises ValueError on decryption failure
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_password_smart_sync(versleuteld_wachtwoord, andere_prive_sleutel)

    def test_decrypt_password_smart_sync_no_key(self, versleuteld_wachtwoord: str):
        """Test behavior when no private key is provided."""
        # API now raises ValueError when no key is available
        with pytest.raises(ValueError, match="no private key available"):
            decrypt_password_smart_sync(versleuteld_wachtwoord, None)

    def test_decrypt_password_smart_sync_plain_text(self, prive_sleutel: str):
        """Test handling of plain text passwords."""
        plain_password = "plain:simple_password"
        result = decrypt_password_smart_sync(plain_password, prive_sleutel)

        # Should return the content without prefix
        assert result == "simple_password"

    def test_configmap_password_integration(self, prive_sleutel: str, versleuteld_wachtwoord: str):
        """De base64+age-vorm uit de configmap en het armored blok erin openen hetzelfde."""
        result = decrypt_password_smart_sync(versleuteld_wachtwoord, prive_sleutel)
        armored = base64.b64decode(versleuteld_wachtwoord.removeprefix("base64+age:")).decode("utf-8")

        assert is_age_encrypted(armored)
        assert decrypt_age_content_sync(armored, prive_sleutel) == result
