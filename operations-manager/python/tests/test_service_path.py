"""Tests for service-aware path resolution."""

from __future__ import annotations

import copy

import pytest
from opi.forms.editables.service_path import (
    ensure_service_in_list,
    find_service_in_list,
    is_service_config_path,
    parse_service_path,
    smart_get_value,
    smart_set_value,
)

# ---------------------------------------------------------------------------
# Sample data mimicking real project YAML
# ---------------------------------------------------------------------------

SAMPLE_SERVICES_DATA: dict = {
    "name": "test-project",
    "services": [
        "publish-on-web",
        {
            "keycloak": {
                "config": {
                    "template": "sso-only",
                    "additional_redirect_uris": [
                        "http://localhost:8080/*",
                    ],
                    "restrict-access": {
                        "enabled": True,
                        "realm-role": "allowed-user",
                    },
                },
            },
        },
        "authorization-wall",
        {
            "namespace-postgresql-database": {
                "config": {
                    "instances": 2,
                    "storage": "10Gi",
                },
            },
        },
    ],
    "config": {
        "age-public-key": "age1abc",
    },
}


class TestIsServiceConfigPath:
    def test_keycloak_template(self):
        assert is_service_config_path("services/keycloak/config/template") is True

    def test_keycloak_nested(self):
        assert is_service_config_path("services/keycloak/config/restrict-access/enabled") is True

    def test_postgresql(self):
        assert is_service_config_path("services/namespace-postgresql-database/config/instances") is True

    def test_top_level_services(self):
        assert is_service_config_path("services") is False

    def test_non_service_path(self):
        assert is_service_config_path("config/age-public-key") is False

    def test_users_path(self):
        assert is_service_config_path("users[0]/email") is False

    def test_service_name_only(self):
        assert is_service_config_path("services/keycloak") is True


class TestParseServicePath:
    def test_full_path(self):
        name, sub = parse_service_path("services/keycloak/config/template")
        assert name == "keycloak"
        assert sub == "config/template"

    def test_deep_path(self):
        name, sub = parse_service_path("services/keycloak/config/restrict-access/enabled")
        assert name == "keycloak"
        assert sub == "config/restrict-access/enabled"

    def test_service_only(self):
        name, sub = parse_service_path("services/keycloak")
        assert name == "keycloak"
        assert sub is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Not a service config path"):
            parse_service_path("config/age-public-key")


class TestFindServiceInList:
    def test_find_string_entry(self):
        services = ["publish-on-web", "keycloak"]
        idx, entry = find_service_in_list(services, "publish-on-web")
        assert idx == 0
        assert entry == "publish-on-web"

    def test_find_dict_entry(self):
        services = ["publish-on-web", {"keycloak": {"config": {"template": "sso-only"}}}]
        idx, entry = find_service_in_list(services, "keycloak")
        assert idx == 1
        assert isinstance(entry, dict)
        assert "keycloak" in entry

    def test_not_found(self):
        services = ["publish-on-web"]
        idx, entry = find_service_in_list(services, "keycloak")
        assert idx == -1
        assert entry is None

    def test_empty_list(self):
        idx, entry = find_service_in_list([], "keycloak")
        assert idx == -1
        assert entry is None


class TestEnsureServiceInList:
    def test_promotes_string_to_dict(self):
        services: list = ["keycloak", "publish-on-web"]
        idx, entry = ensure_service_in_list(services, "keycloak")
        assert idx == 0
        # RC-5 A2.3: promotion produces the uniform record form
        assert entry == {"name": "keycloak"}
        assert services[0] == {"name": "keycloak"}

    def test_existing_dict_unchanged(self):
        original = {"keycloak": {"config": {"template": "sso-only"}}}
        services: list = [original]
        idx, entry = ensure_service_in_list(services, "keycloak")
        assert idx == 0
        assert entry is original

    def test_creates_new_entry(self):
        services: list = ["publish-on-web"]
        idx, entry = ensure_service_in_list(services, "keycloak")
        assert idx == 1
        assert entry == {"name": "keycloak"}
        assert len(services) == 2


class TestSmartGetValue:
    def test_keycloak_template(self):
        assert smart_get_value(SAMPLE_SERVICES_DATA, "services/keycloak/config/template") == "sso-only"

    def test_keycloak_redirect_uris(self):
        result = smart_get_value(SAMPLE_SERVICES_DATA, "services/keycloak/config/additional_redirect_uris")
        assert result == ["http://localhost:8080/*"]

    def test_keycloak_nested_config(self):
        assert smart_get_value(SAMPLE_SERVICES_DATA, "services/keycloak/config/restrict-access/enabled") is True
        assert (
            smart_get_value(SAMPLE_SERVICES_DATA, "services/keycloak/config/restrict-access/realm-role")
            == "allowed-user"
        )

    def test_postgresql_config(self):
        assert smart_get_value(SAMPLE_SERVICES_DATA, "services/namespace-postgresql-database/config/instances") == 2

    def test_nonexistent_service(self):
        assert smart_get_value(SAMPLE_SERVICES_DATA, "services/vault/config/enabled") is None

    def test_nonexistent_config_key(self):
        assert smart_get_value(SAMPLE_SERVICES_DATA, "services/keycloak/config/nonexistent") is None

    def test_string_service_returns_none(self):
        assert smart_get_value(SAMPLE_SERVICES_DATA, "services/publish-on-web/config/something") is None

    def test_non_service_path_delegates(self):
        assert smart_get_value(SAMPLE_SERVICES_DATA, "config/age-public-key") == "age1abc"
        assert smart_get_value(SAMPLE_SERVICES_DATA, "name") == "test-project"

    def test_top_level_services_delegates(self):
        result = smart_get_value(SAMPLE_SERVICES_DATA, "services")
        assert isinstance(result, list)

    def test_no_services_key(self):
        assert smart_get_value({"name": "test"}, "services/keycloak/config/template") is None

    def test_service_name_only(self):
        result = smart_get_value(SAMPLE_SERVICES_DATA, "services/keycloak")
        assert isinstance(result, dict)
        assert "config" in result


class TestSmartSetValue:
    def test_set_existing_config(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        smart_set_value(data, "services/keycloak/config/template", "sso-support")
        assert smart_get_value(data, "services/keycloak/config/template") == "sso-support"

    def test_set_new_config_key(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        smart_set_value(data, "services/keycloak/config/new-key", "new-value")
        assert smart_get_value(data, "services/keycloak/config/new-key") == "new-value"

    def test_set_nested_config(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        smart_set_value(data, "services/keycloak/config/restrict-access/error-message", "Access denied")
        assert smart_get_value(data, "services/keycloak/config/restrict-access/error-message") == "Access denied"

    def test_promotes_string_service(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        smart_set_value(data, "services/authorization-wall/config/banner", "Welcome!")
        assert smart_get_value(data, "services/authorization-wall/config/banner") == "Welcome!"
        # Other services should be unaffected
        assert smart_get_value(data, "services/keycloak/config/template") == "sso-only"

    def test_creates_new_service(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        smart_set_value(data, "services/vault/config/enabled", True)
        assert smart_get_value(data, "services/vault/config/enabled") is True

    def test_preserves_other_entries(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        original_len = len(data["services"])
        smart_set_value(data, "services/keycloak/config/template", "new-value")
        assert len(data["services"]) == original_len

    def test_non_service_path_delegates(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        smart_set_value(data, "config/age-public-key", "new-key")
        assert data["config"]["age-public-key"] == "new-key"

    def test_creates_services_list_if_missing(self):
        data: dict = {"name": "test"}
        smart_set_value(data, "services/keycloak/config/template", "sso-only")
        assert isinstance(data["services"], list)
        assert smart_get_value(data, "services/keycloak/config/template") == "sso-only"

    def test_round_trip(self):
        """smart_set then smart_get returns the same value."""
        data: dict = {"services": ["keycloak"]}
        smart_set_value(data, "services/keycloak/config/template", "sso-only")
        assert smart_get_value(data, "services/keycloak/config/template") == "sso-only"

    def test_set_service_entire_config(self):
        data = copy.deepcopy(SAMPLE_SERVICES_DATA)
        new_config = {"template": "algoritmeregister"}
        smart_set_value(data, "services/keycloak", new_config)
        assert smart_get_value(data, "services/keycloak") == new_config
