from __future__ import annotations

from unittest.mock import patch

from opi.forms.editables.converters import (
    AGEEncryptConverter,
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
from opi.forms.editables.generators import UserEnvVarsEncryptGenerator
from opi.forms.editables.validators import KeyValueValidator

FAKE_AGE_ENCRYPTED = "-----BEGIN AGE ENCRYPTED FILE-----\nencrypted\n-----END AGE ENCRYPTED FILE-----"
FAKE_PUBLIC_KEY = "age1testpublickey"


class TestEncryptedDisplayConverter:
    def test_view_age_encrypted(self):
        conv = EncryptedDisplayConverter()
        assert conv.view("-----BEGIN AGE ENCRYPTED FILE-----\ndata") == "Versleuteld opgeslagen"

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
        result = KeyValueConverter().write("KEY=value\nOTHER=val2")
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_yaml_text_returns_empty(self):
        """write() with default dict mode only parses KEY=value, not YAML."""
        result = KeyValueConverter().write("KEY: value\nOTHER: val2")
        assert result == {}  # no KEY=value lines found, returns empty dict

    def test_write_skips_comments(self):
        result = KeyValueConverter().write("# comment\nKEY=value")
        assert result == {"KEY": "value"}

    def test_write_skips_empty_lines(self):
        result = KeyValueConverter().write("KEY=value\n\nOTHER=val2")
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_dict_passthrough(self):
        """Dict input is returned as-is."""
        result = KeyValueConverter().write({"KEY": "value"})
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
        errors = KeyValueValidator().validate("BADLINE")
        assert len(errors) == 1
        assert "BADLINE" in errors[0]

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

        with patch("opi.utils.age.encrypt_age_content_sync", return_value=FAKE_AGE_ENCRYPTED) as mock:
            result = conv.write("DB_HOST=localhost", context_data=yaml_data)

        mock.assert_called_once_with("DB_HOST=localhost", FAKE_PUBLIC_KEY)
        assert "BEGIN AGE ENCRYPTED FILE" in str(result)

    def test_write_string_skips_already_encrypted(self):
        """Already-encrypted values must not be double-encrypted."""
        conv = KeyValueConverter(fmt="env", write_as="string")
        yaml_data = {"config": {"age-public-key": FAKE_PUBLIC_KEY}}

        with patch("opi.utils.age.encrypt_age_content_sync") as mock:
            result = conv.write(FAKE_AGE_ENCRYPTED, context_data=yaml_data)

        mock.assert_not_called()
        assert result == FAKE_AGE_ENCRYPTED

    def test_write_string_without_yaml_data_returns_plain(self):
        """Without yaml_data, encryption cannot happen - value is returned as-is."""
        conv = KeyValueConverter(fmt="env", write_as="string")
        result = conv.write("SECRET=value")
        assert result == "SECRET=value"

    def test_write_string_without_public_key_returns_plain(self):
        """Without a project public key in context_data, value is returned as-is."""
        conv = KeyValueConverter(fmt="env", write_as="string")
        result = conv.write("SECRET=value", context_data={"config": {}})
        assert result == "SECRET=value"

    def test_write_dict_mode_does_not_encrypt(self):
        """write_as='dict' mode should never attempt encryption."""
        conv = KeyValueConverter(fmt="env", write_as="dict")
        yaml_data = {"config": {"age-public-key": FAKE_PUBLIC_KEY}}

        with patch("opi.utils.age.encrypt_age_content_sync") as mock:
            result = conv.write("KEY=value", context_data=yaml_data)

        mock.assert_not_called()
        assert isinstance(result, dict)


class TestUserEnvVarsEncryptGenerator:
    """Verify that the create-wizard generator encrypts all component user-env-vars."""

    def test_encrypts_plain_user_env_vars(self):
        """Plain-text user-env-vars on components must be encrypted."""
        yaml_data = {
            "config": {"age-public-key": FAKE_PUBLIC_KEY},
            "components": [
                {"name": "frontend", "user-env-vars": "API_URL=https://api.example.nl"},
                {"name": "backend", "user-env-vars": "DB_PASS=secret123"},
            ],
        }

        with patch("opi.utils.age.encrypt_age_content_sync", return_value=FAKE_AGE_ENCRYPTED):
            UserEnvVarsEncryptGenerator().generate(yaml_data)

        for comp in yaml_data["components"]:
            assert "BEGIN AGE ENCRYPTED FILE" in comp["user-env-vars"], (
                f"user-env-vars for {comp['name']} should be encrypted"
            )

    def test_skips_already_encrypted(self):
        """Already-encrypted user-env-vars must not be re-encrypted."""
        yaml_data = {
            "config": {"age-public-key": FAKE_PUBLIC_KEY},
            "components": [{"name": "frontend", "user-env-vars": FAKE_AGE_ENCRYPTED}],
        }

        with patch("opi.utils.age.encrypt_age_content_sync") as mock:
            UserEnvVarsEncryptGenerator().generate(yaml_data)

        mock.assert_not_called()

    def test_skips_empty_user_env_vars(self):
        """Components without user-env-vars should be left alone."""
        yaml_data = {
            "config": {"age-public-key": FAKE_PUBLIC_KEY},
            "components": [
                {"name": "frontend"},
                {"name": "backend", "user-env-vars": ""},
            ],
        }

        with patch("opi.utils.age.encrypt_age_content_sync") as mock:
            UserEnvVarsEncryptGenerator().generate(yaml_data)

        mock.assert_not_called()

    def test_skips_when_no_public_key(self):
        """Without a project public key, generator should skip (not crash)."""
        yaml_data = {
            "config": {},
            "components": [{"name": "frontend", "user-env-vars": "SECRET=value"}],
        }

        with patch("opi.utils.age.encrypt_age_content_sync") as mock:
            UserEnvVarsEncryptGenerator().generate(yaml_data)

        mock.assert_not_called()
        # Value stays plain - this is a known limitation when no key exists
        assert yaml_data["components"][0]["user-env-vars"] == "SECRET=value"


class TestAGEEncryptConverter:
    """Verify that AGEEncryptConverter encrypts/decrypts field values."""

    def test_write_encrypts_plain_value(self):
        """Plain text must be encrypted with the system AGE key."""
        conv = AGEEncryptConverter(public_key=FAKE_PUBLIC_KEY)

        with patch("opi.utils.age.encrypt_age_content_sync", return_value=FAKE_AGE_ENCRYPTED):
            result = conv.write("my-secret-value")

        assert "BEGIN AGE ENCRYPTED FILE" in result

    def test_write_skips_already_encrypted(self):
        """Already-encrypted values must not be double-encrypted."""
        conv = AGEEncryptConverter(public_key=FAKE_PUBLIC_KEY)

        with patch("opi.utils.age.encrypt_age_content_sync") as mock:
            result = conv.write(FAKE_AGE_ENCRYPTED)

        mock.assert_not_called()
        assert result == FAKE_AGE_ENCRYPTED

    def test_write_empty_returns_empty(self):
        """Empty values should return empty string, not attempt encryption."""
        conv = AGEEncryptConverter(public_key=FAKE_PUBLIC_KEY)
        assert conv.write("") == ""
        assert conv.write(None) == ""

    def test_read_decrypts_encrypted_value(self):
        """Encrypted values should be decrypted for form display."""
        conv = AGEEncryptConverter(public_key=FAKE_PUBLIC_KEY)

        with (
            patch("opi.utils.age.decrypt_age_content_sync", return_value="decrypted-secret"),
            patch("opi.core.config.settings") as mock_settings,
        ):
            mock_settings.SOPS_AGE_PRIVATE_KEY = "AGE-SECRET-KEY-1TEST"
            result = conv.read(FAKE_AGE_ENCRYPTED)

        assert result == "decrypted-secret"

    def test_view_always_masked(self):
        """View should never reveal the actual value."""
        conv = AGEEncryptConverter(public_key=FAKE_PUBLIC_KEY)
        assert conv.view(FAKE_AGE_ENCRYPTED) == "********"
        assert conv.view("plain-text") == "********"
        assert conv.view(None) == "Niet geconfigureerd"
