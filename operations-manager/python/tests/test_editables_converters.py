from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest
from opi.core.config import settings
from opi.forms.editables.converters import (
    CloneFromConverter,
    ContainerImageConverter,
    DeploymentServicesDisplayConverter,
    EncryptedDisplayConverter,
    IntegerListConverter,
    KeycloakRealmsDisplayConverter,
    KeyValueConverter,
    ServiceListConverter,
    TruncateConverter,
)
from opi.forms.editables.validators import KeyValueValidator
from opi.utils.age import AgeDecryptionError, AgeEncryptionError

FAKE_AGE_ENCRYPTED = "-----BEGIN AGE ENCRYPTED FILE-----\nencrypted\n-----END AGE ENCRYPTED FILE-----"
FAKE_PUBLIC_KEY = "age1testpublickey"

#: The context every write needs since RC-463: without a project public key the converter
#: refuses instead of storing the value readable.
PROJECT_WITH_KEY = {"config": {"age-public-key": FAKE_PUBLIC_KEY}}

#: Where the converter looks up the encryption, so a patch has to land on ITS name.
ENCRYPT = "opi.forms.editables.converters.encrypt_age_content_sync"


def _encryption_off():
    """Encrypt as a no-op, so a test about PARSING reads values instead of ciphertext."""
    return patch(ENCRYPT, side_effect=lambda plain, _key: plain)


class TestEncryptedDisplayConverter:
    def test_view_age_encrypted(self):
        conv = EncryptedDisplayConverter()
        assert conv.view(FAKE_AGE_ENCRYPTED) == "Versleuteld opgeslagen"

    def test_view_plain_value(self):
        assert EncryptedDisplayConverter().view("some-value") == "Geconfigureerd"

    def test_view_none(self):
        assert EncryptedDisplayConverter().view(None) == "Niet geconfigureerd"

    def test_view_empty_string(self):
        assert EncryptedDisplayConverter().view("") == "Niet geconfigureerd"

    def test_read_returns_empty(self):
        assert EncryptedDisplayConverter().read("anything") == ""

    def test_write_preserves_original(self):
        original = "-----BEGIN AGE ENCRYPTED FILE-----\ndata"
        assert EncryptedDisplayConverter().write(original) == original


class TestTruncateConverter:
    def test_view_long_string(self):
        conv = TruncateConverter(max_length=10)
        assert conv.view("abcdefghijklmnop") == "abcdefghij..."

    def test_view_short_string(self):
        conv = TruncateConverter(max_length=20)
        assert conv.view("short") == "short"

    def test_view_none(self):
        assert TruncateConverter().view(None) == "Niet geconfigureerd"

    def test_read_write_passthrough(self):
        conv = TruncateConverter()
        assert conv.read("value") == "value"
        assert conv.write("value") == "value"


class TestServiceListConverter:
    def test_read_mixed_list(self):
        value = ["publish-on-web", {"keycloak": {"config": {"template": "sso-support"}}}]
        result = ServiceListConverter().read(value)
        assert "publish-on-web" in result
        assert "keycloak" in result

    def test_read_empty(self):
        assert ServiceListConverter().read(None) == []
        assert ServiceListConverter().read([]) == []

    def test_write_simple_list(self):
        assert ServiceListConverter().write(["a", "b"]) == ["a", "b"]

    def test_view_matches_read(self):
        value = ["publish-on-web"]
        conv = ServiceListConverter()
        assert conv.view(value) == conv.read(value)

    def test_component_scope_never_restores_catalog_data(self):
        # A component's services must never receive the project attachments catalog data,
        # or the whole catalog gets duplicated onto the component.
        ctx = {"services": [{"attachments": {"data": [{"id": "ca-root", "filename": "r.pem", "content": "x"}]}}]}
        assert ServiceListConverter().write(["attachments"], ctx) == ["attachments"]

    def test_project_scope_restores_catalog_data(self):
        # The project services list keeps the uploaded attachments catalog on save.
        ctx = {"services": [{"attachments": {"data": [{"id": "ca-root", "filename": "r.pem", "content": "x"}]}}]}
        result = ServiceListConverter(preserve_catalog_data=True).write(["attachments"], ctx)
        assert result[0]["attachments"]["data"][0]["id"] == "ca-root"


class TestIntegerListConverter:
    def test_read_list_to_string(self):
        assert IntegerListConverter().read([80, 443]) == "80, 443"

    def test_write_string_to_list(self):
        assert IntegerListConverter().write("80, 443") == [80, 443]

    def test_round_trip(self):
        conv = IntegerListConverter()
        original = [8000, 8080, 443]
        assert conv.write(conv.read(original)) == original

    def test_write_skips_invalid(self):
        assert IntegerListConverter().write("80, abc, 443") == [80, 443]

    def test_read_empty_list(self):
        assert IntegerListConverter().read([]) == ""

    def test_read_none(self):
        assert IntegerListConverter().read(None) == ""


class TestKeyValueConverter:
    def test_read_dict_yaml_format(self):
        """Dict values with yaml fmt produce KEY: value lines."""
        result = KeyValueConverter(fmt="yaml").read({"KEY": "value", "OTHER": "val2"})
        assert "KEY: value" in result
        assert "OTHER: val2" in result

    def test_read_dict_env_format(self):
        """Dict values with env fmt produce KEY=value lines."""
        result = KeyValueConverter(fmt="env").read({"KEY": "value"})
        assert "KEY=value" in result

    def test_read_string_passthrough(self):
        """String values are returned as-is."""
        assert KeyValueConverter().read("KEY=value\nOTHER=val2") == "KEY=value\nOTHER=val2"

    def test_write_env_text_to_dict(self):
        """write() parses KEY=value text into a dict."""
        with _encryption_off():
            result = KeyValueConverter().write("KEY=value\nOTHER=val2", context_data=PROJECT_WITH_KEY)
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_yaml_text_parses_as_dict(self):
        """write() accepts YAML mapping input and parses it into a dict.

        Aliases are conceptually key->template, and users frequently enter
        them in YAML form. The converter delegates to the shared parser
        which auto-detects YAML vs KEY=VALUE, so both formats work.
        """
        with _encryption_off():
            result = KeyValueConverter().write("KEY: value\nOTHER: val2", context_data=PROJECT_WITH_KEY)
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_yaml_text_with_equals_in_value(self):
        """YAML values containing '=' (URL query strings, etc.) must be preserved."""
        text = 'DATABASE_URL: "postgresql://host/db?schema=public"'
        with _encryption_off():
            result = KeyValueConverter().write(text, context_data=PROJECT_WITH_KEY)
        assert result == {"DATABASE_URL": "postgresql://host/db?schema=public"}

    def test_write_yaml_text_with_special_chars(self):
        """YAML values containing ':' '@' '/' '?' inside quotes must be preserved."""
        text = 'CONN: "user:pass@host:5432/db?opt=1"\nURL: "https://api.example.nl/v1"'
        with _encryption_off():
            result = KeyValueConverter().write(text, context_data=PROJECT_WITH_KEY)
        assert result == {
            "CONN": "user:pass@host:5432/db?opt=1",
            "URL": "https://api.example.nl/v1",
        }

    def test_round_trip_yaml_aliases(self):
        """read -> write -> read must produce the same dict (alias round-trip)."""
        original = {
            "DATABASE_URL": "postgresql://$U:$P@$H:5432/$D?schema=$S",
            "NEXTAUTH_URL": "https://$PUBLIC_HOST",
        }
        conv = KeyValueConverter(fmt="env")
        text = conv.read(original)
        with _encryption_off():
            parsed = conv.write(text, context_data=PROJECT_WITH_KEY)
        text2 = conv.read(parsed)
        assert parsed == original
        assert text2 == text

    def test_write_skips_comments(self):
        with _encryption_off():
            result = KeyValueConverter().write("# comment\nKEY=value", context_data=PROJECT_WITH_KEY)
        assert result == {"KEY": "value"}

    def test_write_skips_empty_lines(self):
        with _encryption_off():
            result = KeyValueConverter().write("KEY=value\n\nOTHER=val2", context_data=PROJECT_WITH_KEY)
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_dict_passthrough(self):
        """Dict input keeps its keys; only the values go through the encryption."""
        with _encryption_off():
            result = KeyValueConverter().write({"KEY": "value"}, context_data=PROJECT_WITH_KEY)
        assert result == {"KEY": "value"}

    def test_view_matches_read(self):
        conv = KeyValueConverter()
        assert conv.view("KEY=value") == conv.read("KEY=value")

    def test_default_format_is_env(self):
        conv = KeyValueConverter()
        assert conv.fmt == "env"


class TestContainerImageConverter:
    def test_write_lowercases(self):
        result = ContainerImageConverter().write("Nginx:Latest")
        assert result == "nginx:latest"

    def test_write_strips_whitespace(self):
        result = ContainerImageConverter().write("  nginx:latest  ")
        assert result == "nginx:latest"

    def test_write_empty_returns_none(self):
        assert ContainerImageConverter().write("") is None
        assert ContainerImageConverter().write(None) is None

    def test_read_returns_string(self):
        assert ContainerImageConverter().read("nginx:latest") == "nginx:latest"
        assert ContainerImageConverter().read(None) == ""


class TestCloneFromConverter:
    def test_read_dict_extracts_reference(self):
        value = {"type": "deployment", "reference": "staging", "mode": "once"}
        assert CloneFromConverter().read(value) == "staging"

    def test_read_string_passthrough(self):
        assert CloneFromConverter().read("staging") == "staging"

    def test_read_none_returns_empty(self):
        assert CloneFromConverter().read(None) == ""

    def test_write_string_to_dict(self):
        result = CloneFromConverter().write("staging")
        assert result == {"type": "deployment", "reference": "staging", "mode": "once"}

    def test_write_empty_returns_none(self):
        assert CloneFromConverter().write("") is None
        assert CloneFromConverter().write(None) is None
        assert CloneFromConverter().write("  ") is None

    def test_write_dict_passthrough(self):
        value = {"type": "deployment", "reference": "staging", "mode": "once"}
        assert CloneFromConverter().write(value) == value

    def test_view_completed(self):
        value = {"reference": "prod", "type": "remote-source", "status": {"completed": True, "timestamp": "2026-02-03"}}
        result = CloneFromConverter().view(value)
        assert "prod" in result
        assert "Voltooid" in result

    def test_view_in_progress(self):
        value = {"reference": "prod", "type": "remote-source", "status": {}}
        result = CloneFromConverter().view(value)
        assert "Bezig" in result

    def test_view_none(self):
        assert CloneFromConverter().view(None) == ""

    def test_view_string(self):
        assert CloneFromConverter().view("staging") == "staging"


class TestDeploymentServicesDisplayConverter:
    def test_view_with_services(self):
        value = [{"reference": "minio-storage"}, {"reference": "redis"}]
        result = DeploymentServicesDisplayConverter().view(value)
        assert "minio-storage" in result
        assert "redis" in result

    def test_view_empty(self):
        assert DeploymentServicesDisplayConverter().view([]) == "Geen deployment services"
        assert DeploymentServicesDisplayConverter().view(None) == "Geen deployment services"


class TestKeycloakRealmsDisplayConverter:
    def test_view_with_realms(self):
        value = [{"host": "https://kc.example.nl", "realm": "my-realm", "username": "admin"}]
        result = KeycloakRealmsDisplayConverter().view(value)
        assert len(result) == 1
        assert result[0]["realm"] == "my-realm"

    def test_view_empty(self):
        assert KeycloakRealmsDisplayConverter().view(None) == []
        assert KeycloakRealmsDisplayConverter().view([]) == []


class TestKeyValueValidator:
    """Validates ENV and YAML key-value input via validate_and_parse_env_vars."""

    # --- Valid ENV ---

    def test_valid_env_single(self):
        assert KeyValueValidator().validate("KEY=value") == []

    def test_valid_env_multi(self):
        assert KeyValueValidator().validate("KEY=value\nOTHER=val2") == []

    def test_valid_env_with_comment(self):
        assert KeyValueValidator().validate("# comment\nKEY=value") == []

    def test_valid_env_equals_in_value(self):
        assert KeyValueValidator().validate("KEY=val=ue") == []

    def test_valid_env_empty_value(self):
        assert KeyValueValidator().validate("KEY=") == []

    # --- Valid YAML ---

    def test_valid_yaml_single(self):
        assert KeyValueValidator().validate("KEY: value") == []

    def test_valid_yaml_multi(self):
        assert KeyValueValidator().validate("KEY: value\nOTHER: val2") == []

    def test_valid_yaml_integer_value(self):
        assert KeyValueValidator().validate("PORT: 8080") == []

    def test_valid_yaml_boolean_value(self):
        assert KeyValueValidator().validate("DEBUG: true") == []

    def test_valid_yaml_pipe_block(self):
        assert KeyValueValidator().validate("CONFIG: |\n  line1\n  line2") == []

    def test_valid_yaml_folded_block(self):
        assert KeyValueValidator().validate("CONFIG: >\n  line1\n  line2") == []

    # --- Empty / None ---

    def test_empty_string(self):
        assert KeyValueValidator().validate("") == []

    def test_none(self):
        assert KeyValueValidator().validate(None) == []

    def test_whitespace_only(self):
        assert KeyValueValidator().validate("  ") == []

    # --- Invalid ---

    def test_invalid_no_separator(self):
        # The message points at the line by NUMBER, not by quoting it back: the same
        # parser message is logged and returned to the client on the save path, where the
        # line may be a pasted secret (RC-25 security rework).
        errors = KeyValueValidator().validate("BADLINE")
        assert len(errors) == 1
        assert "Line 1" in errors[0]
        assert "BADLINE" not in errors[0]

    def test_invalid_env_line_in_multi(self):
        errors = KeyValueValidator().validate("KEY=value\nBADLINE")
        assert len(errors) == 1

    def test_invalid_env_key_starts_with_digit(self):
        errors = KeyValueValidator().validate("123BAD=value")
        assert len(errors) == 1

    def test_invalid_yaml_unclosed_flow(self):
        errors = KeyValueValidator().validate("KEY: [unclosed")
        assert len(errors) == 1

    def test_invalid_yaml_list_value(self):
        """Lists are not valid env var values - only scalars allowed."""
        errors = KeyValueValidator().validate("ITEMS:\n  - one\n  - two")
        assert len(errors) == 1


class TestKeyValueConverterEncryption:
    """Verify that KeyValueConverter(write_as='string') encrypts user-env-vars."""

    def test_write_string_encrypts_with_project_key(self):
        """Plain text must be AGE-encrypted when yaml_data has a project public key."""
        conv = KeyValueConverter(fmt="env", write_as="string")
        yaml_data = {"config": {"age-public-key": FAKE_PUBLIC_KEY}}

        with patch(ENCRYPT, return_value=FAKE_AGE_ENCRYPTED) as mock:
            result = conv.write("DB_HOST=localhost", context_data=yaml_data)

        mock.assert_called_once_with("DB_HOST=localhost", FAKE_PUBLIC_KEY)
        assert "BEGIN AGE ENCRYPTED FILE" in str(result)

    def test_write_string_skips_already_encrypted(self):
        """Already-encrypted values must not be double-encrypted."""
        conv = KeyValueConverter(fmt="env", write_as="string")
        yaml_data = {"config": {"age-public-key": FAKE_PUBLIC_KEY}}

        with patch(ENCRYPT) as mock:
            result = conv.write(FAKE_AGE_ENCRYPTED, context_data=yaml_data)

        mock.assert_not_called()
        assert result == FAKE_AGE_ENCRYPTED

    def test_write_string_without_context_data_refuses(self):
        """No context means no key, and the value holds a secret: refuse rather than store it."""
        conv = KeyValueConverter(fmt="env", write_as="string")
        with pytest.raises(AgeEncryptionError):
            conv.write("SECRET=value")

    def test_write_string_without_public_key_refuses(self):
        conv = KeyValueConverter(fmt="env", write_as="string")
        with pytest.raises(AgeEncryptionError):
            conv.write("SECRET=value", context_data={"config": {}})

    def test_write_dict_mode_encrypts_each_value(self):
        """write_as='dict' (aliases) must AGE-encrypt each value, keeping names readable."""
        conv = KeyValueConverter(fmt="env", write_as="dict")
        yaml_data = {"config": {"age-public-key": FAKE_PUBLIC_KEY}}

        with patch(ENCRYPT, return_value=FAKE_AGE_ENCRYPTED) as mock:
            result = conv.write("KEY=value", context_data=yaml_data)

        mock.assert_called_once_with("value", FAKE_PUBLIC_KEY)
        assert isinstance(result, dict)
        assert "KEY" in result
        assert "BEGIN AGE ENCRYPTED FILE" in str(result["KEY"])

    def test_write_dict_mode_skips_already_encrypted(self):
        """Already-encrypted alias values must not be double-encrypted."""
        conv = KeyValueConverter(fmt="env", write_as="dict")
        yaml_data = {"config": {"age-public-key": FAKE_PUBLIC_KEY}}

        with patch(ENCRYPT) as mock:
            result = conv.write({"KEY": FAKE_AGE_ENCRYPTED}, context_data=yaml_data)

        mock.assert_not_called()
        assert result == {"KEY": FAKE_AGE_ENCRYPTED}

    def test_write_dict_mode_without_public_key_refuses(self):
        conv = KeyValueConverter(fmt="env", write_as="dict")
        with pytest.raises(AgeEncryptionError):
            conv.write("KEY=value", context_data={"config": {}})

    def test_read_dict_mode_decrypts_each_value(self):
        """Reading aliases must decrypt each AGE-encrypted value for editor display."""
        conv = KeyValueConverter(fmt="env", write_as="dict")
        stored = {"KEY": FAKE_AGE_ENCRYPTED, "PLAIN": "just-text"}

        with (
            patch("opi.utils.age.decrypt_age_content_sync", return_value="decrypted"),
            patch("opi.forms.editables.converters.project_private_key_or_none", return_value="AGE-SECRET-KEY-1TEST"),
        ):
            result = conv.read(stored, context_data={"config": {}})

        assert "KEY=decrypted" in result
        assert "PLAIN=just-text" in result


OLD_CIPHERTEXT = "-----BEGIN AGE ENCRYPTED FILE-----\nb2xk\n-----END AGE ENCRYPTED FILE-----"
NEW_CIPHERTEXT = "-----BEGIN AGE ENCRYPTED FILE-----\nbmV3\n-----END AGE ENCRYPTED FILE-----"
ENCODED_PROJECT_KEY = "-----BEGIN AGE ENCRYPTED FILE-----\na2V5\n-----END AGE ENCRYPTED FILE-----"
PROJECT_PRIVATE_KEY = "AGE-SECRET-KEY-1PROJECT"
CONTEXT = {"config": {"age-private-key": ENCODED_PROJECT_KEY}}


def _plaintexts(new_plaintext: str) -> dict[str, str]:
    """What the two entry points below are supposed to open: the project key and both values."""
    return {
        ENCODED_PROJECT_KEY: PROJECT_PRIVATE_KEY,
        OLD_CIPHERTEXT: "FOO=bar",
        NEW_CIPHERTEXT: new_plaintext,
    }


def _fake_decrypt(new_plaintext: str):
    """Stub for the variant that answers None when a key does not fit."""
    table = _plaintexts(new_plaintext)

    def decrypt(content: str, key: str, **_kwargs: object) -> str | None:
        return table.get(content)

    return decrypt


def _fake_decrypt_strict(new_plaintext: str):
    """Stub for the core that raises; the project-key unwrap runs through that one."""
    table = _plaintexts(new_plaintext)

    def decrypt(content: str, key: str, **_kwargs: object) -> str:
        plain = table.get(content)
        if plain is None:
            raise AgeDecryptionError("Age decryption failed: no matching keys found")
        return plain

    return decrypt


def _patched_age(new_plaintext: str):
    """Both decrypt paths plus the settings the project-key unwrap reads."""
    return (
        patch("opi.utils.age.decrypt_age_content_sync", side_effect=_fake_decrypt(new_plaintext)),
        patch("opi.utils.age._decrypt_block", side_effect=_fake_decrypt_strict(new_plaintext)),
        patch.object(settings, "SOPS_AGE_PRIVATE_KEY", "AGE-SECRET-KEY-1SYSTEM"),
        patch.object(settings, "PROJECT_KEK_PRIVATE_KEY", None),
    )


class TestKeepExistingCiphertextIfUnchanged:
    def test_keeps_existing_when_plaintext_identical(self):
        from opi.forms.editables.converters import keep_existing_ciphertext_if_unchanged

        with ExitStack() as stack:
            for patcher in _patched_age("FOO=bar"):
                stack.enter_context(patcher)
            result = keep_existing_ciphertext_if_unchanged(OLD_CIPHERTEXT, NEW_CIPHERTEXT, CONTEXT)

        assert result == OLD_CIPHERTEXT

    def test_returns_new_when_plaintext_differs(self):
        from opi.forms.editables.converters import keep_existing_ciphertext_if_unchanged

        with ExitStack() as stack:
            for patcher in _patched_age("FOO=changed"):
                stack.enter_context(patcher)
            result = keep_existing_ciphertext_if_unchanged(OLD_CIPHERTEXT, NEW_CIPHERTEXT, CONTEXT)

        assert result == NEW_CIPHERTEXT

    def test_returns_new_when_no_existing_value(self):
        from opi.forms.editables.converters import keep_existing_ciphertext_if_unchanged

        assert keep_existing_ciphertext_if_unchanged(None, NEW_CIPHERTEXT, CONTEXT) == NEW_CIPHERTEXT

    def test_returns_new_when_existing_not_encrypted(self):
        from opi.forms.editables.converters import keep_existing_ciphertext_if_unchanged

        assert keep_existing_ciphertext_if_unchanged("FOO=bar", NEW_CIPHERTEXT, CONTEXT) == NEW_CIPHERTEXT

    def test_returns_new_when_key_unresolvable(self):
        from opi.forms.editables.converters import keep_existing_ciphertext_if_unchanged

        result = keep_existing_ciphertext_if_unchanged(OLD_CIPHERTEXT, NEW_CIPHERTEXT, {})
        assert result == NEW_CIPHERTEXT

    def test_returns_new_when_decrypt_fails(self):
        from opi.forms.editables.converters import keep_existing_ciphertext_if_unchanged

        with ExitStack() as stack:
            mock_decrypt, *_ = [stack.enter_context(patcher) for patcher in _patched_age("FOO=bar")]
            # Unknown ciphertext (e.g. rotated key) decrypts to None
            unknown = "-----BEGIN AGE ENCRYPTED FILE-----\ncm90YXRlZA==\n-----END AGE ENCRYPTED FILE-----"
            result = keep_existing_ciphertext_if_unchanged(unknown, NEW_CIPHERTEXT, CONTEXT)

        assert result == NEW_CIPHERTEXT
        assert mock_decrypt.called


class TestWriteFieldCiphertextReuse:
    """Integration: _write_field keeps stored ciphertext for untouched env vars."""

    def _editable(self):
        from opi.forms.editables.editable import Editable

        return Editable(
            yaml_path="components[*]/user-env-vars",
            converter=KeyValueConverter(write_as="string"),
        )

    def _data(self):
        return {
            "config": {
                "age-private-key": ENCODED_PROJECT_KEY,
                "age-public-key": FAKE_PUBLIC_KEY,
            },
            "components": [{"name": "web", "user-env-vars": OLD_CIPHERTEXT}],
        }

    def test_unchanged_plaintext_keeps_stored_ciphertext(self):
        from opi.forms.editables.processor import EditableFormProcessor

        data = self._data()
        with ExitStack() as stack:
            stack.enter_context(patch(ENCRYPT, return_value=NEW_CIPHERTEXT))
            for patcher in _patched_age("FOO=bar"):
                stack.enter_context(patcher)
            EditableFormProcessor._write_field(self._editable(), "components[0]/user-env-vars", "FOO=bar", data)

        assert data["components"][0]["user-env-vars"] == OLD_CIPHERTEXT

    def test_changed_plaintext_writes_new_ciphertext(self):
        from opi.forms.editables.processor import EditableFormProcessor

        data = self._data()
        with ExitStack() as stack:
            stack.enter_context(patch(ENCRYPT, return_value=NEW_CIPHERTEXT))
            for patcher in _patched_age("FOO=changed"):
                stack.enter_context(patcher)
            EditableFormProcessor._write_field(self._editable(), "components[0]/user-env-vars", "FOO=changed", data)

        assert data["components"][0]["user-env-vars"] == NEW_CIPHERTEXT
