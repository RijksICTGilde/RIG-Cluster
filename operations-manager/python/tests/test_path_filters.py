"""Tests for extended path syntax: {K} dict-key and {F=V} field-match filters."""

from opi.forms.editables.path import get_value, resolve_path, set_value

# ---------------------------------------------------------------------------
# Sample data matching v2 project YAML
# ---------------------------------------------------------------------------


def _sample_component():
    return {
        "components": [
            {
                "name": "frontend",
                "services": [
                    "publish-on-web",
                    "keycloak",
                    {
                        "persistent-storage": {
                            "config": [
                                {"name": "data", "size": "250Mi", "mount-path": "/data"},
                                {"name": "uploads", "size": "1Gi", "mount-path": "/uploads"},
                            ],
                        }
                    },
                    {
                        "temp-storage": {
                            "config": [
                                {"name": "cache", "size": "100Mi", "mount-path": "/tmp/cache"},
                            ],
                        }
                    },
                ],
            },
        ],
    }


def _sample_services():
    return {
        "services": [
            "publish-on-web",
            {"keycloak": {"config": {"template": "sso-only"}}},
            "persistent-storage",
            {"namespace-postgresql-database": {"config": {"instances": 2, "storage": "5Gi"}}},
        ],
    }


# ===================================================================
# get_value — {K} dict-key filter
# ===================================================================


class TestGetValueDictKeyFilter:
    def test_find_dict_entry_in_mixed_list(self):
        data = _sample_services()
        result = get_value(data, "services{keycloak}/config/template")
        assert result == "sso-only"

    def test_find_storage_config(self):
        data = _sample_component()
        result = get_value(data, "components[0]/services{persistent-storage}/config")
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "data"

    def test_find_storage_item_name(self):
        data = _sample_component()
        result = get_value(data, "components[0]/services{persistent-storage}/config[0]/name")
        assert result == "data"

    def test_find_storage_with_wildcard(self):
        data = _sample_component()
        result = get_value(data, "components[*]/services{persistent-storage}/config[*]/name")
        assert result == [["data", "uploads"]]

    def test_missing_key_returns_none(self):
        data = _sample_services()
        result = get_value(data, "services{nonexistent}/config")
        assert result is None

    def test_string_entry_has_no_subpath(self):
        """String entries like 'publish-on-web' don't have nested config."""
        data = _sample_services()
        result = get_value(data, "services{publish-on-web}/config")
        assert result is None

    def test_temp_storage_config(self):
        data = _sample_component()
        result = get_value(data, "components[0]/services{temp-storage}/config[0]/name")
        assert result == "cache"


# ===================================================================
# get_value — {F=V} field-match filter
# ===================================================================


class TestGetValueFieldMatchFilter:
    def test_find_by_name_field(self):
        data = _sample_component()
        result = get_value(data, "components[0]/services{persistent-storage}/config{name=data}/size")
        assert result == "250Mi"

    def test_find_second_item(self):
        data = _sample_component()
        result = get_value(data, "components[0]/services{persistent-storage}/config{name=uploads}/mount-path")
        assert result == "/uploads"

    def test_missing_field_value_returns_none(self):
        data = _sample_component()
        result = get_value(data, "components[0]/services{persistent-storage}/config{name=nonexistent}/size")
        assert result is None

    def test_find_component_by_name(self):
        data = {
            "deployments": [
                {
                    "name": "staging",
                    "components": [
                        {"reference": "frontend", "image": "nginx:latest"},
                        {"reference": "api", "image": "python:3.13"},
                    ],
                }
            ],
        }
        result = get_value(data, "deployments[0]/components{reference=frontend}/image")
        assert result == "nginx:latest"

    def test_find_component_by_name_in_wildcard(self):
        data = {
            "components": [
                {"name": "frontend", "image": "nginx"},
                {"name": "api", "image": "python"},
            ],
        }
        result = get_value(data, "components{name=api}/image")
        assert result == "python"


# ===================================================================
# set_value — {K} dict-key filter
# ===================================================================


class TestSetValueDictKeyFilter:
    def test_set_existing_service_config(self):
        data = _sample_services()
        set_value(data, "services{keycloak}/config/template", "full")
        assert get_value(data, "services{keycloak}/config/template") == "full"

    def test_promote_string_to_dict(self):
        """Setting a config on a string service should promote it to a dict."""
        data = {"services": ["publish-on-web", "keycloak"]}
        set_value(data, "services{keycloak}/config/template", "sso-only")
        result = get_value(data, "services{keycloak}/config/template")
        assert result == "sso-only"
        # Original string should be replaced by dict
        assert data["services"][1] == {"keycloak": {"config": {"template": "sso-only"}}}

    def test_create_new_entry(self):
        """Setting a value for a non-existent service should create it."""
        data = {"services": ["publish-on-web"]}
        set_value(data, "services{redis}/config/port", 6379)
        assert get_value(data, "services{redis}/config/port") == 6379
        assert len(data["services"]) == 2

    def test_set_storage_config(self):
        data = _sample_component()
        set_value(data, "components[0]/services{persistent-storage}/config[0]/size", "500Mi")
        assert get_value(data, "components[0]/services{persistent-storage}/config[0]/size") == "500Mi"

    def test_create_services_list_if_missing(self):
        data = {"components": [{"name": "web"}]}
        set_value(data, "components[0]/services{keycloak}/config/template", "basic")
        assert get_value(data, "components[0]/services{keycloak}/config/template") == "basic"

    def test_set_terminal_filter_value(self):
        """Setting a value directly at a {K} filter (terminal position)."""
        data = {"services": ["publish-on-web", {"keycloak": {"old": True}}]}
        set_value(data, "services{keycloak}", {"new": True})
        assert data["services"][1] == {"keycloak": {"new": True}}


# ===================================================================
# set_value — {F=V} field-match filter
# ===================================================================


class TestSetValueFieldMatchFilter:
    def test_set_field_on_existing_item(self):
        data = _sample_component()
        set_value(data, "components[0]/services{persistent-storage}/config{name=data}/size", "500Mi")
        result = get_value(data, "components[0]/services{persistent-storage}/config{name=data}/size")
        assert result == "500Mi"

    def test_create_new_named_item(self):
        data = _sample_component()
        set_value(data, "components[0]/services{persistent-storage}/config{name=logs}/size", "2Gi")
        result = get_value(data, "components[0]/services{persistent-storage}/config{name=logs}/size")
        assert result == "2Gi"
        # Should have been appended
        configs = get_value(data, "components[0]/services{persistent-storage}/config")
        assert len(configs) == 3
        assert configs[2]["name"] == "logs"

    def test_set_terminal_field_match(self):
        """Replace an entire matched dict at terminal position."""
        data = {"items": [{"id": "a", "val": 1}, {"id": "b", "val": 2}]}
        set_value(data, "items{id=a}", {"id": "a", "val": 99})
        assert data["items"][0] == {"id": "a", "val": 99}

    def test_create_missing_entry_at_terminal(self):
        data = {"items": [{"id": "a", "val": 1}]}
        set_value(data, "items{id=b}", {"id": "b", "val": 2})
        assert len(data["items"]) == 2
        assert data["items"][1] == {"id": "b", "val": 2}


# ===================================================================
# Combined: chained filters
# ===================================================================


class TestChainedFilters:
    def test_dict_key_then_field_match(self):
        """services{persistent-storage}/config{name=data}/size — full chain."""
        data = _sample_component()
        path = "components[0]/services{persistent-storage}/config{name=data}/size"
        assert get_value(data, path) == "250Mi"
        set_value(data, path, "1Gi")
        assert get_value(data, path) == "1Gi"

    def test_wildcard_with_dict_key_filter(self):
        """components[*] + services{K} — wildcard before filter."""
        data = _sample_component()
        result = get_value(data, "components[*]/services{persistent-storage}/config[0]/name")
        assert result == ["data"]


# ===================================================================
# Backward compatibility
# ===================================================================


class TestBackwardCompatibility:
    """Existing path syntax still works exactly as before."""

    def test_simple_key(self):
        assert get_value({"name": "test"}, "name") == "test"

    def test_nested_key(self):
        assert get_value({"a": {"b": {"c": 1}}}, "a/b/c") == 1

    def test_list_index(self):
        assert get_value({"items": [10, 20, 30]}, "items[1]") == 20

    def test_wildcard(self):
        data = {"users": [{"name": "a"}, {"name": "b"}]}
        assert get_value(data, "users[*]/name") == ["a", "b"]

    def test_set_simple(self):
        data = {"name": "old"}
        set_value(data, "name", "new")
        assert data["name"] == "new"

    def test_set_nested_creates_intermediates(self):
        data: dict = {}
        set_value(data, "a/b/c", 1)
        assert data == {"a": {"b": {"c": 1}}}

    def test_set_list_index(self):
        data: dict = {}
        set_value(data, "items[0]/name", "first")
        assert data["items"][0]["name"] == "first"

    def test_resolve_path_unchanged(self):
        assert resolve_path("a[*]/b[*]", 0) == "a[0]/b[*]"
        assert resolve_path("a[*]/b{K}/c[*]", 1) == "a[1]/b{K}/c[*]"

    def test_missing_path_returns_none(self):
        assert get_value({}, "a/b/c") is None
        assert get_value({"a": None}, "a/b") is None
