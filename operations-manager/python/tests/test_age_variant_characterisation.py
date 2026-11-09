"""What each AGE variant does today, pinned before the variants are merged (RC-453).

Covers the paths that had no test: the wrong key per stored form and per variant, plaintext
that is not utf-8, the project-key lookups, and the fail-open converters. A behaviour that
is deliberately kept is marked as such; the rest is a starting point a migration must not
move by accident.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

import pyrage
import pytest
from opi.forms.editables.converters import KeyValueConverter
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.catalog.image_registries.converters import ProjectAgeSecretConverter
from opi.utils.age import (
    AgeDecryptionError,
    carries_encrypted_value,
    decrypt_age_block_to_bytes,
    decrypt_age_content_sync,
    decrypt_password_smart,
    decrypt_password_smart_sync,
    encrypt_age_content_sync,
    encrypt_file_to_age_block_sync,
    generate_age_key_pair,
    get_decoded_project_private_key_sync,
    is_age_encrypted,
    project_private_key_or_none,
    wrap_project_key_sync,
)


def _key_pair() -> tuple[str, str]:
    """A throwaway pair, generated in process so the test needs no age binary."""
    identity = pyrage.x25519.Identity.generate()
    return str(identity), str(identity.to_public())


PRIVATE_KEY, PUBLIC_KEY = _key_pair()
OTHER_PRIVATE_KEY, OTHER_PUBLIC_KEY = _key_pair()
KEK_PRIVATE_KEY, KEK_PUBLIC_KEY = _key_pair()


@pytest.fixture
def kek(monkeypatch: pytest.MonkeyPatch) -> str:
    """Settings with only a project-KEK, so the platform key is never the one that fits."""
    from opi.core.config import settings

    monkeypatch.setattr(settings, "PROJECT_KEK_PRIVATE_KEY", KEK_PRIVATE_KEY, raising=False)
    monkeypatch.setattr(settings, "PROJECT_KEK_PUBLIC_KEY", KEK_PUBLIC_KEY, raising=False)
    monkeypatch.setattr(settings, "SOPS_AGE_PRIVATE_KEY", None, raising=False)
    monkeypatch.setattr(settings, "SOPS_AGE_PUBLIC_KEY", None, raising=False)
    return KEK_PRIVATE_KEY


def _stored(form: str, public_key: str) -> str:
    """The same secret in the form the project file would carry it."""
    block = encrypt_age_content_sync("secret", public_key)
    return block if form == "block" else "base64+age:" + base64.b64encode(block.encode()).decode()


def _non_utf8_block() -> str:
    """A block whose plaintext is not text, which only pyrage can produce here."""
    recipient = pyrage.x25519.Recipient.from_str(PUBLIC_KEY)
    return pyrage.encrypt(b"\xff\xfe", [recipient], armored=True).decode("ascii").strip()


def _project_data(private_key: str = PRIVATE_KEY, public_key: str = PUBLIC_KEY) -> dict[str, Any]:
    return {
        "name": "demo",
        "config": {
            "age-public-key": public_key,
            "age-private-key": encrypt_age_content_sync(private_key, KEK_PUBLIC_KEY),
        },
    }


class TestWrongKeyPerForm:
    """One answer for "this key does not open it", whatever the form or the variant."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("form", ["block", "base64+age"])
    async def test_async_raises_age_decryption_error(self, form: str) -> None:
        with pytest.raises(AgeDecryptionError, match="Age decryption failed"):
            await decrypt_password_smart(_stored(form, OTHER_PUBLIC_KEY), PRIVATE_KEY)

    @pytest.mark.parametrize("form", ["block", "base64+age"])
    def test_sync_raises_age_decryption_error(self, form: str) -> None:
        with pytest.raises(AgeDecryptionError, match="Age decryption failed"):
            decrypt_password_smart_sync(_stored(form, OTHER_PUBLIC_KEY), PRIVATE_KEY)

    def test_age_decryption_error_is_a_value_error(self) -> None:
        """So a caller that only knows ``except ValueError`` does not miss it."""
        assert issubclass(AgeDecryptionError, ValueError)

    def test_sync_block_decrypt_returns_none(self) -> None:
        block = encrypt_age_content_sync("secret", OTHER_PUBLIC_KEY)
        assert decrypt_age_content_sync(block, PRIVATE_KEY) is None

    def test_a_damaged_base64_payload_stays_a_decode_error(self) -> None:
        with pytest.raises(ValueError, match="Failed to decode base64 content"):
            decrypt_password_smart_sync("base64+age:!!not base64!!", PRIVATE_KEY)


class TestNonUtf8Plaintext:
    """Plaintext that is not utf-8 is a decryption failure, not a stray UnicodeDecodeError."""

    def test_sync_returns_none(self) -> None:
        assert decrypt_age_content_sync(_non_utf8_block(), PRIVATE_KEY) is None

    @pytest.mark.asyncio
    async def test_async_raises_age_decryption_error(self) -> None:
        with pytest.raises(AgeDecryptionError):
            await decrypt_password_smart(_non_utf8_block(), PRIVATE_KEY)


class TestAttachmentBlock:
    @pytest.mark.asyncio
    async def test_round_trip(self) -> None:
        block = encrypt_file_to_age_block_sync(b"\x00binary\xff", PUBLIC_KEY)
        assert await decrypt_age_block_to_bytes(block, PRIVATE_KEY) == b"\x00binary\xff"

    @pytest.mark.asyncio
    async def test_wrong_key(self) -> None:
        block = encrypt_file_to_age_block_sync(b"payload", OTHER_PUBLIC_KEY)
        with pytest.raises(AgeDecryptionError):
            await decrypt_age_block_to_bytes(block, PRIVATE_KEY)


class TestProjectKeyLookups:
    """Four lookups, three answers for the same missing or unusable key."""

    def test_fail_closed_lookup_raises_without_key(self, kek: str) -> None:
        with pytest.raises(ValueError, match="Missing age-private-key"):
            get_decoded_project_private_key_sync({"config": {}})

    def test_fail_closed_lookup_raises_on_wrong_kek(self, kek: str) -> None:
        data = {"config": {"age-private-key": encrypt_age_content_sync(PRIVATE_KEY, OTHER_PUBLIC_KEY)}}
        with pytest.raises(ValueError, match="Could not unwrap the project private key"):
            get_decoded_project_private_key_sync(data)

    def test_fail_closed_lookup_returns_the_key(self, kek: str) -> None:
        assert get_decoded_project_private_key_sync(_project_data()) == PRIVATE_KEY

    def test_lenient_lookup_returns_none_without_key(self, kek: str) -> None:
        assert project_private_key_or_none({"config": {}}) is None
        assert project_private_key_or_none(None) is None

    def test_lenient_lookup_returns_none_on_wrong_kek(self, kek: str) -> None:
        data = {"config": {"age-private-key": encrypt_age_content_sync(PRIVATE_KEY, OTHER_PUBLIC_KEY)}}
        assert project_private_key_or_none(data) is None

    def test_lenient_lookup_returns_the_key(self, kek: str) -> None:
        assert project_private_key_or_none(_project_data()) == PRIVATE_KEY


class TestHandlerDecryptWrapper:
    """``_decrypt_with_private_key``: one form in, text out."""

    def test_escaped_newlines_are_normalised(self) -> None:
        block = encrypt_age_content_sync("hello", PUBLIC_KEY)
        handler = ProjectFileHandler()
        assert handler._decrypt_with_private_key(block.replace("\n", "\\n"), PRIVATE_KEY) == "hello"

    def test_base64_age_form_is_accepted(self) -> None:
        block = encrypt_age_content_sync("hello", PUBLIC_KEY)
        prefixed = "base64+age:" + base64.b64encode(block.encode()).decode()
        handler = ProjectFileHandler()
        assert handler._decrypt_with_private_key(prefixed, PRIVATE_KEY) == "hello"

    @pytest.mark.parametrize("plaintext", ["FOO=bar\nBAZ=qux", "123", "a: b", "true", "null"])
    def test_the_plaintext_comes_back_as_text(self, plaintext: str) -> None:
        """Not YAML-parsed: ``"123"`` is a value, not an int, and ``"a: b"`` is not a dict."""
        block = encrypt_age_content_sync(plaintext, PUBLIC_KEY)
        handler = ProjectFileHandler()
        assert handler._decrypt_with_private_key(block, PRIVATE_KEY) == plaintext

    def test_wrong_key_raises(self) -> None:
        block = encrypt_age_content_sync("hello", OTHER_PUBLIC_KEY)
        handler = ProjectFileHandler()
        with pytest.raises(AgeDecryptionError, match="Age decryption failed"):
            handler._decrypt_with_private_key(block, PRIVATE_KEY)

    def test_missing_key_raises(self) -> None:
        handler = ProjectFileHandler()
        with pytest.raises(ValueError, match="Private key is required"):
            handler._decrypt_with_private_key("whatever", None)


class TestFailOpenConverters:
    """Deliberately kept (RC-453): making these fail-closed is a security decision of its own."""

    def test_key_value_converter_keeps_ciphertext_it_cannot_open(self, kek: str) -> None:
        block = encrypt_age_content_sync("FOO=bar", OTHER_PUBLIC_KEY)
        assert KeyValueConverter._maybe_decrypt(block, _project_data()) == block

    def test_key_value_converter_round_trip_keeps_the_stored_ciphertext(self, kek: str) -> None:
        """A read-then-write of an unreadable value leaves the stored ciphertext standing.

        The form shows the ciphertext, and the write side recognises it as already
        encrypted, so a save does not turn it into a block around a block.
        """
        block = encrypt_age_content_sync("FOO=bar", OTHER_PUBLIC_KEY)
        data = _project_data()
        shown = KeyValueConverter._maybe_decrypt(block, data)
        assert KeyValueConverter._maybe_encrypt(shown, data) == block

    def test_key_value_converter_writes_plaintext_without_a_public_key(self) -> None:
        assert KeyValueConverter._maybe_encrypt("FOO=bar", {"config": {}}) == "FOO=bar"

    def test_registry_converter_writes_plaintext_without_a_public_key(self) -> None:
        assert ProjectAgeSecretConverter().write("ghp_token", {"config": {}}) == "ghp_token"

    @pytest.mark.asyncio
    async def test_aliases_stay_a_plain_mapping_without_a_public_key(self) -> None:
        from opi.utils.project_utils import build_component_config

        config = await build_component_config(
            name="component-1",
            component_type="single",
            port=8080,
            path="/",
            services=[],
            aliases="FOO: $BAR",
            public_key=None,
        )
        assert config["aliases"] == {"FOO": "$BAR"}

    @pytest.mark.asyncio
    async def test_env_vars_are_dropped_without_a_public_key(self) -> None:
        from opi.utils.project_utils import build_component_config

        config = await build_component_config(
            name="component-1",
            component_type="single",
            port=8080,
            path="/",
            services=[],
            env_vars="FOO=bar",
            public_key=None,
        )
        assert "user-env-vars" not in config


class TestRecognition:
    """``is_age_encrypted`` is strict where a substring check is not."""

    @pytest.mark.parametrize(
        "value",
        [
            "see below:\n-----BEGIN AGE ENCRYPTED FILE-----\nYWJj\n-----END AGE ENCRYPTED FILE-----",
            "-----BEGIN AGE ENCRYPTED FILE-----\nYWJj\n-----END AGE ENCRYPTED FILE-----\ntrailing",
        ],
    )
    def test_strict_rejects_a_block_that_is_not_the_whole_value(self, value: str) -> None:
        assert "BEGIN AGE ENCRYPTED FILE" in value
        assert not is_age_encrypted(value)
        assert not carries_encrypted_value(value)

    def test_escaped_newlines_still_count_as_a_block(self) -> None:
        """Only the first and the last line are looked at, so the escaped form passes."""
        escaped = "-----BEGIN AGE ENCRYPTED FILE-----\\nYWJj\\n-----END AGE ENCRYPTED FILE-----"
        assert is_age_encrypted(escaped)

    def test_prefixed_form_is_not_an_armored_block(self) -> None:
        prefixed = "base64+age:" + base64.b64encode(b"x").decode()
        assert not is_age_encrypted(prefixed)
        assert carries_encrypted_value(prefixed)


class TestKeyGeneration:
    """Generating a key pair starts no process, so it cannot hold the event loop."""

    def test_no_subprocess_is_started(self) -> None:
        with patch("subprocess.run", side_effect=AssertionError("a key pair needs no process")):
            private_key, public_key = generate_age_key_pair()
        assert private_key.startswith("AGE-SECRET-KEY-")
        assert public_key.startswith("age1")

    def test_the_pair_opens_what_it_encrypts(self) -> None:
        private_key, public_key = generate_age_key_pair()
        block = encrypt_age_content_sync("secret", public_key)
        assert decrypt_age_content_sync(block, private_key) == "secret"

    def test_every_pair_is_its_own(self) -> None:
        assert generate_age_key_pair() != generate_age_key_pair()


class TestKeyFileFormat:
    """The stored key file round-trips through the parser that reads it back."""

    def test_round_trip_is_byte_exact(self) -> None:
        from opi.core.config import format_age_key_file, parse_sops_age_key_content

        content = format_age_key_file(PRIVATE_KEY, PUBLIC_KEY)
        assert parse_sops_age_key_content(content) == (PUBLIC_KEY, PRIVATE_KEY)
        assert content.endswith("\n")

    def test_a_pyrage_key_opens_what_the_age_helpers_wrote(self) -> None:
        wrapped = wrap_project_key_sync(PRIVATE_KEY, PUBLIC_KEY)
        assert decrypt_age_content_sync(wrapped, PRIVATE_KEY) == PRIVATE_KEY
