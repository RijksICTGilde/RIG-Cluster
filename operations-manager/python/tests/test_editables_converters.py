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
    def test_read_dict_to_env_string(self):
        result = KeyValueConverter(fmt="env").read({"KEY": "value", "OTHER": "val2"})
        assert "KEY=value" in result
        assert "OTHER=val2" in result

    def test_read_dict_to_yaml_string(self):
        result = KeyValueConverter(fmt="yaml").read({"KEY": "value", "OTHER": "val2"})
        assert "KEY: value" in result
        assert "OTHER: val2" in result

    def test_write_env_format(self):
        result = KeyValueConverter().write("KEY=value\nOTHER=val2")
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_yaml_format(self):
        result = KeyValueConverter().write("KEY: value\nOTHER: val2")
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_skips_comments(self):
        result = KeyValueConverter().write("# comment\nKEY=value")
        assert result == {"KEY": "value"}

    def test_write_skips_empty_lines(self):
        result = KeyValueConverter().write("KEY=value\n\nOTHER=val2")
        assert result == {"KEY": "value", "OTHER": "val2"}

    def test_write_handles_equals_in_value(self):
        result = KeyValueConverter().write("KEY=val=ue")
        assert result == {"KEY": "val=ue"}

    def test_write_handles_colon_in_value(self):
        result = KeyValueConverter().write("URL: http://example.com")
        assert result == {"URL": "http://example.com"}

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
