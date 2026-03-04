from __future__ import annotations

from opi.forms.editables.converters import (
    CloneFromDisplayConverter,
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
    def test_read_dict_to_yaml_string(self):
        """Legacy dict values are converted to YAML text for editing."""
        result = KeyValueConverter(fmt="yaml").read({"KEY": "value", "OTHER": "val2"})
        assert "KEY: value" in result
        assert "OTHER: val2" in result

    def test_read_dict_legacy_env(self):
        """Legacy dict values with env fmt also produce YAML (yaml.dump)."""
        result = KeyValueConverter(fmt="env").read({"KEY": "value"})
        assert "KEY: value" in result

    def test_read_string_passthrough(self):
        """String values are returned as-is."""
        assert KeyValueConverter().read("KEY=value\nOTHER=val2") == "KEY=value\nOTHER=val2"

    def test_write_preserves_raw_text(self):
        """write() returns the raw text unchanged."""
        text = "KEY=value\nOTHER=val2"
        assert KeyValueConverter().write(text) == text

    def test_write_preserves_yaml(self):
        text = "KEY: value\nOTHER: val2"
        assert KeyValueConverter().write(text) == text

    def test_write_preserves_comments(self):
        text = "# comment\nKEY=value"
        assert KeyValueConverter().write(text) == text

    def test_write_preserves_empty_lines(self):
        text = "KEY=value\n\nOTHER=val2"
        assert KeyValueConverter().write(text) == text

    def test_write_preserves_pipe_blocks(self):
        text = "CONFIG: |\n  line1\n  line2"
        assert KeyValueConverter().write(text) == text

    def test_write_strips_whitespace(self):
        assert KeyValueConverter().write("  KEY=value  ") == "KEY=value"

    def test_write_dict_to_yaml(self):
        """Legacy dict input is converted to YAML text."""
        result = KeyValueConverter().write({"KEY": "value"})
        assert "KEY: value" in result

    def test_view_matches_read(self):
        conv = KeyValueConverter()
        assert conv.view("KEY=value") == conv.read("KEY=value")

    def test_default_format_is_env(self):
        conv = KeyValueConverter()
        assert conv.fmt == "env"

    def test_detect_format_env(self):
        assert KeyValueConverter().detect_format("KEY=value\nOTHER=val2") == "env"

    def test_detect_format_yaml(self):
        assert KeyValueConverter().detect_format("KEY: value\nOTHER: val2") == "yaml"

    def test_detect_format_empty(self):
        assert KeyValueConverter(fmt="env").detect_format("") == "env"


class TestContainerImageConverter:
    def test_write_lowercases(self):
        result = ContainerImageConverter().write("Nginx:Latest")
        assert result == "nginx:latest"

    def test_write_strips_whitespace(self):
        result = ContainerImageConverter().write("  nginx:latest  ")
        assert result == "nginx:latest"

    def test_write_empty_returns_empty(self):
        assert ContainerImageConverter().write("") == ""
        assert ContainerImageConverter().write(None) == ""

    def test_read_returns_string(self):
        assert ContainerImageConverter().read("nginx:latest") == "nginx:latest"
        assert ContainerImageConverter().read(None) == ""


class TestCloneFromDisplayConverter:
    def test_view_completed(self):
        value = {"reference": "prod", "type": "remote-source", "status": {"completed": True, "timestamp": "2026-02-03"}}
        result = CloneFromDisplayConverter().view(value)
        assert "prod" in result
        assert "Voltooid" in result

    def test_view_in_progress(self):
        value = {"reference": "prod", "type": "remote-source", "status": {}}
        result = CloneFromDisplayConverter().view(value)
        assert "Bezig" in result

    def test_view_none(self):
        assert CloneFromDisplayConverter().view(None) == ""


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
        """Lists are not valid env var values — only scalars allowed."""
        errors = KeyValueValidator().validate("ITEMS:\n  - one\n  - two")
        assert len(errors) == 1
