"""Unit tests for KeycloakYamlHandler helpers."""

from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler


class TestBuildClientDataFromYaml:
    """Tests for the static YAML-to-client-representation builder."""

    def test_minimal_yaml_applies_defaults(self):
        result = KeycloakYamlHandler._build_client_data_from_yaml({"clientId": "my-client"})

        assert result["clientId"] == "my-client"
        assert result["name"] == "my-client"
        assert result["protocol"] == "openid-connect"
        assert result["enabled"] is True
        assert result["publicClient"] is False
        assert result["standardFlowEnabled"] is True
        assert result["implicitFlowEnabled"] is False
        assert result["directAccessGrantsEnabled"] is False
        assert result["serviceAccountsEnabled"] is False

    def test_yaml_overrides_defaults(self):
        result = KeycloakYamlHandler._build_client_data_from_yaml(
            {
                "clientId": "my-client",
                "name": "Custom Name",
                "enabled": False,
                "implicitFlowEnabled": True,
                "directAccessGrantsEnabled": True,
            }
        )

        assert result["name"] == "Custom Name"
        assert result["enabled"] is False
        assert result["implicitFlowEnabled"] is True
        assert result["directAccessGrantsEnabled"] is True

    def test_confidential_client_generates_secret(self):
        result = KeycloakYamlHandler._build_client_data_from_yaml({"clientId": "my-client", "publicClient": False})

        assert "secret" in result
        assert isinstance(result["secret"], str)
        assert len(result["secret"]) == 32

    def test_public_client_has_no_secret(self):
        result = KeycloakYamlHandler._build_client_data_from_yaml({"clientId": "my-client", "publicClient": True})

        assert "secret" not in result
        assert result["publicClient"] is True

    def test_attributes_dict_copied_through(self):
        attrs = {
            "backchannel.logout.url": "https://example.com/logout",
            "backchannel.logout.session.required": "true",
        }
        result = KeycloakYamlHandler._build_client_data_from_yaml({"clientId": "my-client", "attributes": attrs})

        assert result["attributes"] == attrs
        # Verify it's a copy, not the same dict — mutating result mustn't affect caller's input.
        result["attributes"]["new.key"] = "x"
        assert "new.key" not in attrs

    def test_redirect_uris_filters_none(self):
        result = KeycloakYamlHandler._build_client_data_from_yaml(
            {"clientId": "my-client", "redirectUris": ["https://a.example", None, "https://b.example"]}
        )

        assert result["redirectUris"] == ["https://a.example", "https://b.example"]

    def test_empty_redirect_uris_not_added(self):
        # YAML provides redirectUris but every entry is None — field should be absent
        # (Keycloak rejects empty redirectUris arrays for standard-flow clients).
        result = KeycloakYamlHandler._build_client_data_from_yaml({"clientId": "my-client", "redirectUris": [None]})

        assert "redirectUris" not in result

    def test_web_origins_filters_none(self):
        result = KeycloakYamlHandler._build_client_data_from_yaml(
            {"clientId": "my-client", "webOrigins": ["https://a.example", None, "+"]}
        )

        assert result["webOrigins"] == ["https://a.example", "+"]

    def test_secret_uniqueness(self):
        # Two confidential clients must not share a secret.
        a = KeycloakYamlHandler._build_client_data_from_yaml({"clientId": "a"})
        b = KeycloakYamlHandler._build_client_data_from_yaml({"clientId": "b"})

        assert a["secret"] != b["secret"]
